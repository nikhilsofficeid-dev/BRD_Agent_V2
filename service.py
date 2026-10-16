"""HeyPM service layer: the functions the API (and the chat agent's tools) call.

    svc = get_service()
    await svc.process_meeting("MTG-123")                     # transcript -> Firestore record
    svc.get_record("MTG-123")                                # what the UI shows
    await svc.ask("MTG-123", "What did Anita say about SLAs?")
    await svc.confirm_actions("MTG-123", skip_refs=["A4"])   # PM clicked Confirm: JIRA + MOM email
    svc.list_templates()
    await svc.generate_brd("MTG-123", template_id=None)      # only when the PM asks for a BRD

Every write updates last_modified_date / last_modified_time_stamp.
"""
from __future__ import annotations

import json
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any, Optional

from google.genai import types

from . import config, pipeline
from .stores import Stores, blank_record, build_stores, public_view
from .tools import email_ops, jira_ops
from .tools.docx_render import markdown_to_docx

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


class MeetingNotProcessed(RuntimeError):
    pass


class AlreadyConfirmed(RuntimeError):
    pass


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:50] or "meeting"


def record_context(record: dict) -> str:
    """Compact text view of the record, given to the chat and BRD agents."""
    ad = record.get("agent_data") or {}
    insights = ad.get("insights") or {}
    lines = [
        f"Meeting id: {record.get('meeting_id')}",
        f"Title: {insights.get('meeting_title', '')}",
        f"Date: {insights.get('meeting_date', '')}",
        f"Attendees: {', '.join(a.get('name', '') for a in insights.get('attendees', []))}",
        "", "SUMMARY:", record.get("summary") or "(not generated yet)",
        "", "MOM:", record.get("MOM") or "(not generated yet)",
        "", f"JIRA actions required: {record.get('jira_actions_requirend')}",
        "JIRA ACTION DESCRIPTION:", record.get("jira_action_description") or "(none)",
        "", f"Relevant JIRA: {', '.join(record.get('relevant_jira') or []) or '(none)'}",
        f"User confirmation provided: {record.get('user_confirmation_provided')}",
    ]
    results = ad.get("jira_results") or {}
    if results:
        lines.append("JIRA execution results:")
        lines += [f"- {ref}: {r.get('status')} {r.get('action', '')} {r.get('key', '')}".rstrip()
                  for ref, r in results.items()]
    if ad.get("mom_email"):
        lines.append(f"MOM email: {json.dumps(ad['mom_email'])}")
    lines.append(f"BRD generated: {record.get('bdr_generated')}"
                 + (f" ({(ad.get('brd') or {}).get('docx_uri')})" if record.get("bdr_generated") else ""))
    return "\n".join(lines)


class HeyPMService:
    def __init__(self, stores: Optional[Stores] = None):
        self.stores = stores or build_stores()
        self._chat_runner = None

    # ------------------------------------------------------------------ reads
    def get_record(self, meeting_id: str, include_agent_data: bool = False) -> Optional[dict]:
        record = self.stores.meetings.get(meeting_id)
        if record is None:
            return None
        return record if include_agent_data else public_view(record)

    def get_transcript(self, meeting_id: str) -> str:
        return self.stores.transcripts.get(meeting_id).text

    def list_templates(self) -> list[dict]:
        return [{"id": t.id, "name": t.name, "source": t.source, "is_default": t.is_default}
                for t in self.stores.templates.list()]

    # ---------------------------------------------------------------- process
    async def process_meeting(self, meeting_id: str, force: bool = False) -> dict:
        """Transcript (GCS) -> summary, MOM, JIRA plan -> Firestore record. Returns the public record.

        Re-processing a meeting whose actions were already confirmed needs force=True; it keeps the
        executed JIRA results and created keys, and resets user_confirmation_provided.
        """
        existing = self.stores.meetings.get(meeting_id)
        if existing and existing.get("user_confirmation_provided") and not force:
            raise AlreadyConfirmed(f"Meeting {meeting_id} already has confirmed actions; pass force=True to redo.")

        transcript = self.stores.transcripts.get(meeting_id)
        state = await pipeline.run_workflow(pipeline.meeting_processing, {
            "transcript": transcript.text,
            "jira_project_key": config.jira_project_key(),
        })
        insights = state.get("insights") or {}
        plan = state.get("jira_plan") or {"project_key": config.jira_project_key(), "actions": []}
        if not insights or not state.get("summary") or not state.get("mom_draft"):
            raise RuntimeError("Processing did not produce insights, summary and MOM; check the model logs.")

        record = existing or blank_record(meeting_id)
        prior = record.get("agent_data") or {}
        created_before = jira_ops.created_keys(prior.get("jira_results") or {})
        record.update({
            "summary": state["summary"],
            "MOM": state["mom_draft"],
            "jira_actions_requirend": bool(plan.get("actions")),
            "jira_action_description": jira_ops.plan_description(plan),
            "relevant_jira": _unique(jira_ops.existing_keys(plan) + created_before),
            "user_confirmation_provided": False,
        })
        record["agent_data"] = {
            **prior,
            "status": "processed",
            "transcript_uri": transcript.source_uri,
            "insights": insights,
            "jira_plan": plan,
            "draft_warnings": state.get("draft_warnings") or [],
            "processed_at": config.now().isoformat(timespec="seconds"),
        }
        self.stores.meetings.put(record)
        return public_view(record)

    # ---------------------------------------------------------------- confirm
    async def confirm_actions(self, meeting_id: str, skip_refs: Optional[list[str]] = None,
                              send_email: bool = True, email_overrides: Optional[dict] = None) -> dict:
        """The PM confirmed: execute the JIRA plan and email the MOM.

        Safe to call again: completed JIRA actions are not repeated and the MOM is sent once.
        `skip_refs` lists plan refs (A1, A2, ...) the PM unticked. Returns the full record.
        """
        record = self._processed(meeting_id)
        ad = record["agent_data"]
        insights = ad.get("insights") or {}

        jira = jira_ops.execute_plan(ad.get("jira_plan") or {}, set(skip_refs or []),
                                     ad.get("jira_results") or {}, insights.get("meeting_title", meeting_id))
        ad["jira_results"] = jira.get("results") or {}
        ad["jira_status"] = jira["status"]
        ad["jira_message"] = jira.get("message")
        ad["skipped_refs"] = sorted(set(skip_refs or []))

        if send_email and jira["status"] != "error":
            already = (ad.get("mom_email") or {}).get("status") in {"sent", "dry_run"}
            if not already:
                ad["mom_email"] = email_ops.send_mom(insights, record.get("MOM", ""), email_overrides)

        if jira["status"] != "error":
            record["user_confirmation_provided"] = True
            ad["confirmed_at"] = config.now().isoformat(timespec="seconds")
            ad["status"] = "confirmed"
        record["relevant_jira"] = _unique((record.get("relevant_jira") or [])
                                          + jira_ops.created_keys(ad["jira_results"]))
        self.stores.meetings.put(record)
        return record

    # -------------------------------------------------------------------- BRD
    async def generate_brd(self, meeting_id: str, template_id: Optional[str] = None) -> dict:
        """Creates the BRD from the chosen (or default) template, then sets bdr_generated=True."""
        record = self.stores.meetings.get(meeting_id)
        if not record or not (record.get("agent_data") or {}).get("insights"):
            await self.process_meeting(meeting_id)
            record = self.stores.meetings.get(meeting_id)
        ad = record["agent_data"]
        template = self.stores.templates.get(template_id)
        transcript = self.stores.transcripts.get(meeting_id)

        state = await pipeline.run_workflow(pipeline.brd_writer, {
            "transcript": transcript.text,
            "insights": ad.get("insights") or {},
            "brd_template": template.content,
            "meeting_record_context": record_context(record),
        })
        brd_md = state.get("brd_markdown") or ""
        if len(brd_md.strip()) < 40:
            raise RuntimeError("BRD generation returned no usable content.")

        stem = f"BRD-{_slug((ad.get('insights') or {}).get('meeting_title', meeting_id))}-{config.now():%Y%m%d-%H%M}"
        with tempfile.TemporaryDirectory() as tmp:
            docx_path = markdown_to_docx(brd_md, Path(tmp) / f"{stem}.docx")
            docx_uri = self.stores.files.save(f"{meeting_id}/{stem}.docx", docx_path.read_bytes(), DOCX_MIME)
        md_uri = self.stores.files.save(f"{meeting_id}/{stem}.md", brd_md.encode("utf-8"), "text/markdown")

        record["bdr_generated"] = True
        ad["brd"] = {
            "docx_uri": docx_uri,
            "md_uri": md_uri,
            "template_id": template.id,
            "template_name": template.name,
            "generated_at": config.now().isoformat(timespec="seconds"),
            "warnings": state.get("draft_warnings") or [],
        }
        self.stores.meetings.put(record)
        return record

    # ------------------------------------------------------------------- chat
    def _chat(self):
        if self._chat_runner is None:
            from google.adk.runners import Runner
            from google.adk.sessions import InMemorySessionService

            from .agent import app
            self._chat_sessions = InMemorySessionService()
            self._chat_runner = Runner(app=app, session_service=self._chat_sessions)
        return self._chat_runner, self._chat_sessions

    async def ask(self, meeting_id: str, question: str, user_id: str = "pm",
                  session_id: Optional[str] = None) -> dict:
        """One chat turn about a meeting. Reuse session_id to keep the conversation going.

        Returns {"session_id", "answer", "pending_confirmation"}; pending_confirmation is set when the
        user asked the chat to create JIRA items, and is answered with `answer_confirmation`.
        For production, construct the Runner with a persistent session service instead (see README).
        """
        runner, sessions = self._chat()
        if session_id is None or await sessions.get_session(app_name=runner.app_name, user_id=user_id,
                                                            session_id=session_id) is None:
            session = await sessions.create_session(app_name=runner.app_name, user_id=user_id,
                                                    session_id=session_id or str(uuid.uuid4()),
                                                    state={"meeting_id": meeting_id})
            session_id = session.id
        msg = types.Content(role="user", parts=[types.Part(text=question)])
        return await self._run_chat(user_id, session_id, msg)

    async def answer_confirmation(self, session_id: str, confirmation_id: str, confirmed: bool,
                                  payload: Optional[dict] = None, user_id: str = "pm") -> dict:
        msg = types.Content(role="user", parts=[types.Part(function_response=types.FunctionResponse(
            id=confirmation_id, name="adk_request_confirmation",
            response={"confirmed": confirmed, "payload": payload or {}}))])
        return await self._run_chat(user_id, session_id, msg)

    async def _run_chat(self, user_id: str, session_id: str, msg: types.Content) -> dict:
        runner, _ = self._chat()
        answer, pending = [], None
        async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=msg):
            for fc in event.get_function_calls():
                if fc.name == "adk_request_confirmation":
                    tc = (fc.args or {}).get("toolConfirmation") or {}
                    pending = {"confirmation_id": fc.id, "hint": tc.get("hint"), "payload": tc.get("payload")}
            if event.content and event.content.parts and not event.get_function_calls() \
                    and not event.get_function_responses():
                text = "".join(p.text or "" for p in event.content.parts if not getattr(p, "thought", False))
                if text.strip():
                    answer.append(text.strip())
        return {"session_id": session_id, "answer": "\n\n".join(answer[-1:]), "pending_confirmation": pending}

    # ---------------------------------------------------------------- helpers
    def _processed(self, meeting_id: str) -> dict:
        record = self.stores.meetings.get(meeting_id)
        if not record or not (record.get("agent_data") or {}).get("jira_plan"):
            raise MeetingNotProcessed(f"Meeting {meeting_id} has not been processed yet.")
        return record


def _unique(items: list[Any]) -> list[Any]:
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


_service: Optional[HeyPMService] = None


def get_service() -> HeyPMService:
    global _service
    if _service is None:
        _service = HeyPMService()
    return _service


def set_service(service: Optional[HeyPMService]) -> None:
    """For tests or custom wiring."""
    global _service
    _service = service
