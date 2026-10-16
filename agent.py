"""HeyPM chat agent (the chat area): answers questions about the open meeting and acts on request.

Session state:
  meeting_id               set by the API when it creates the chat session (or via open_meeting)
  transcript               loaded from GCS on the first turn
  meeting_record_context   refreshed from Firestore every turn (so UI-side changes are visible)

Tools: open_meeting, process_meeting, confirm_jira_actions (asks the PM to confirm),
       list_brd_templates, generate_brd
"""
from __future__ import annotations

from typing import Optional

from google.adk.agents import LlmAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App, ResumabilityConfig
from google.adk.tools.tool_context import ToolContext
from google.genai import types

from . import config, prompts
from .service import AlreadyConfirmed, MeetingNotProcessed, get_service, record_context
from .stores import TemplateNotFound, TranscriptNotFound
from .tools import jira_ops


# ---------------------------------------------------------------------------
# Context loading
# ---------------------------------------------------------------------------
def _load_meeting(state, meeting_id: str, reload_transcript: bool = False) -> Optional[dict]:
    svc = get_service()
    if reload_transcript or state.get("transcript_for") != meeting_id:
        state["transcript"] = svc.get_transcript(meeting_id)
        state["transcript_for"] = meeting_id
    record = svc.get_record(meeting_id, include_agent_data=True)
    state["meeting_id"] = meeting_id
    state["meeting_record_context"] = record_context(record) if record else "(meeting not processed yet)"
    return record


def refresh_context(callback_context: CallbackContext) -> Optional[types.Content]:
    meeting_id = callback_context.state.get("meeting_id")
    if not meeting_id:
        return None
    try:
        _load_meeting(callback_context.state, meeting_id)
    except TranscriptNotFound as exc:
        callback_context.state["transcript"] = ""
        callback_context.state["meeting_record_context"] = f"(transcript not found: {exc})"
    return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
def open_meeting(meeting_id: str, tool_context: ToolContext) -> dict:
    """Opens a meeting in this chat: loads its transcript and stored record.

    Args:
        meeting_id: The meeting's id (as used for the transcript file and the Firestore document).
    """
    try:
        record = _load_meeting(tool_context.state, meeting_id, reload_transcript=True)
    except TranscriptNotFound as exc:
        return {"status": "error", "message": str(exc)}
    return {"status": "ok", "meeting_id": meeting_id, "processed": bool(record),
            "user_confirmation_provided": bool(record and record.get("user_confirmation_provided")),
            "bdr_generated": bool(record and record.get("bdr_generated"))}


async def process_meeting(force: bool, tool_context: ToolContext) -> dict:
    """Generates (or regenerates) the summary, MOM and proposed JIRA actions for the open meeting.

    Args:
        force: Regenerate even if the PM already confirmed this meeting's actions. Use false unless the user insists.
    """
    meeting_id = tool_context.state.get("meeting_id")
    if not meeting_id:
        return {"status": "error", "message": "No meeting is open. Call open_meeting first."}
    try:
        record = await get_service().process_meeting(meeting_id, force=force)
    except AlreadyConfirmed as exc:
        return {"status": "error", "message": str(exc)}
    _load_meeting(tool_context.state, meeting_id)
    return {"status": "ok", "jira_actions_requirend": record["jira_actions_requirend"],
            "relevant_jira": record["relevant_jira"]}


async def confirm_jira_actions(skip_refs: list[str], send_mom_email: bool, tool_context: ToolContext) -> dict:
    """Creates the proposed JIRA items and emails the MOM, after the PM confirms in the chat.

    Args:
        skip_refs: Plan refs the user does not want (e.g. ["A3", "A4"]); empty list for none.
        send_mom_email: Email the MOM to attendees as well. True unless the user said not to.
    """
    svc = get_service()
    meeting_id = tool_context.state.get("meeting_id")
    record = svc.get_record(meeting_id, include_agent_data=True) if meeting_id else None
    plan = (record or {}).get("agent_data", {}).get("jira_plan")
    if not plan:
        return {"status": "error", "message": "This meeting has not been processed yet."}

    confirmation = tool_context.tool_confirmation
    if confirmation is None:
        hint = jira_ops.preview(plan) + ("\n\nThe MOM will also be emailed to the attendees." if send_mom_email else "")
        tool_context.request_confirmation(hint=hint, payload={"skip_refs": list(skip_refs or [])})
        return {"status": "pending_approval"}
    if not confirmation.confirmed:
        return {"status": "rejected", "message": "The PM declined. Nothing was changed."}
    payload = confirmation.payload if isinstance(confirmation.payload, dict) else {}
    skips = payload.get("skip_refs", skip_refs) or []
    try:
        updated = await svc.confirm_actions(meeting_id, skip_refs=skips, send_email=send_mom_email)
    except MeetingNotProcessed as exc:
        return {"status": "error", "message": str(exc)}
    _load_meeting(tool_context.state, meeting_id)
    ad = updated["agent_data"]
    return {"status": ad.get("jira_status"), "message": ad.get("jira_message"),
            "relevant_jira": updated["relevant_jira"], "mom_email": ad.get("mom_email"),
            "dry_run": config.dry_run()}


def list_brd_templates() -> dict:
    """Lists the BRD templates available in the Templates tab (plus the built-in default)."""
    return {"templates": get_service().list_templates()}


async def generate_brd(template: str, tool_context: ToolContext) -> dict:
    """Generates a BRD for the open meeting from a template and marks bdr_generated in the record.

    Args:
        template: Template id or name from list_brd_templates; empty string to use the default template.
    """
    meeting_id = tool_context.state.get("meeting_id")
    if not meeting_id:
        return {"status": "error", "message": "No meeting is open. Call open_meeting first."}
    try:
        record = await get_service().generate_brd(meeting_id, template_id=template or None)
    except TemplateNotFound as exc:
        return {"status": "error", "message": str(exc)}
    _load_meeting(tool_context.state, meeting_id)
    brd = record["agent_data"]["brd"]
    return {"status": "ok", "bdr_generated": True, "template": brd["template_name"],
            "docx_uri": brd["docx_uri"], "warnings": brd["warnings"]}


root_agent = LlmAgent(
    name="heypm_assistant",
    model=config.model(),
    description="HeyPM meeting assistant: Q&A over a meeting, JIRA actions, BRDs.",
    instruction=prompts.chat_instruction,
    tools=[open_meeting, process_meeting, confirm_jira_actions, list_brd_templates, generate_brd],
    before_agent_callback=refresh_context,
    generate_content_config=types.GenerateContentConfig(temperature=0.2),
)

app = App(
    name="BRD_Agent_V2",  # must match the folder name for adk web
    root_agent=root_agent,
    resumability_config=ResumabilityConfig(is_resumable=True),
)
