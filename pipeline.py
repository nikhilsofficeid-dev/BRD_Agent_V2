"""Workflow agents used by the service.

meeting_processing (SequentialAgent)
  1. insight_extractor   -> state["insights"]     (MeetingInsights)
  2. drafters (ParallelAgent)
       summary_writer    -> state["summary"]      (Markdown, for the Summary panel)
       mom_writer        -> state["mom_draft"]    (Markdown email body)
       jira_planner      -> state["jira_plan"]    (JiraPlan)

brd_writer (LlmAgent, run on demand)        -> state["brd_markdown"]  (fills the chosen template)
"""
from __future__ import annotations

import re
import uuid
from typing import Optional

from google.adk.agents import BaseAgent, LlmAgent, ParallelAgent, SequentialAgent
from google.adk.agents.callback_context import CallbackContext
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from . import config, prompts
from .schemas import JiraPlan, MeetingInsights

MAX_DRAFT_TOKENS = 16000  # hard stop if the model degenerates (e.g. endless table padding)
_RUNS_OF_SPACES = re.compile(r"[ \t]{2,}")
_DASH_RUNS = re.compile(r"-{4,}")


def clean_markdown(text: str) -> str:
    """Strips code fences, collapses padded table cells and truncates degenerate lines."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    out = []
    for line in text.splitlines():
        if line.lstrip().startswith("|"):
            line = _DASH_RUNS.sub("---", _RUNS_OF_SPACES.sub(" ", line.replace(":---", "---")))
        else:
            line = line.rstrip()
        if len(line) > 5000:
            line = line[:5000].rstrip() + " ...[truncated]"
        out.append(line)
    return "\n".join(out).strip()


def _clean_state_key(key: str, required_marker: Optional[str] = None):
    def callback(callback_context: CallbackContext) -> None:
        state = callback_context.state
        raw = state.get(key) or ""
        cleaned = clean_markdown(raw)
        state[key] = cleaned
        warnings = list(state.get("draft_warnings") or [])
        if len(raw) > 200_000:
            warnings.append(f"{key}: model output was degenerate ({len(raw):,} chars) and was cleaned up.")
        if required_marker and required_marker not in cleaned:
            warnings.append(f"{key}: draft looks incomplete (missing '{required_marker}'). Review before sharing.")
        state["draft_warnings"] = warnings
        return None
    return callback


def _brd_check(callback_context: CallbackContext) -> None:
    """Flags a BRD that lost the template's last section (usually a truncated generation)."""
    state = callback_context.state
    template = state.get("brd_template", "")
    headings = [line.strip("# ").strip() for line in template.splitlines() if re.match(r"^#{2,3}\s", line)]
    marker = headings[-1] if headings else None
    return _clean_state_key("brd_markdown", required_marker=marker)(callback_context)


def _cfg(temperature: float, capped: bool = False) -> types.GenerateContentConfig:
    return types.GenerateContentConfig(temperature=temperature,
                                       max_output_tokens=MAX_DRAFT_TOKENS if capped else None)


insight_extractor = LlmAgent(
    name="insight_extractor",
    model=config.model(),
    description="Extracts structured meeting insights from a raw transcript.",
    instruction=prompts.extractor_instruction,
    output_schema=MeetingInsights,
    output_key="insights",
    include_contents="none",
    generate_content_config=_cfg(0.1),
)

summary_writer = LlmAgent(
    name="summary_writer",
    model=config.model(),
    description="Writes the short AI summary for the meeting panel.",
    instruction=prompts.summary_instruction,
    output_key="summary",
    include_contents="none",
    generate_content_config=_cfg(0.3, capped=True),
    after_agent_callback=_clean_state_key("summary"),
)

mom_writer = LlmAgent(
    name="mom_writer",
    model=config.model(),
    description="Writes the Minutes of Meeting.",
    instruction=prompts.mom_instruction,
    output_key="mom_draft",
    include_contents="none",
    generate_content_config=_cfg(0.3, capped=True),
    after_agent_callback=_clean_state_key("mom_draft"),
)

jira_planner = LlmAgent(
    name="jira_planner",
    model=config.model(),
    description="Plans the JIRA epic, stories, links, transitions and assignments.",
    instruction=prompts.jira_planner_instruction,
    output_schema=JiraPlan,
    output_key="jira_plan",
    include_contents="none",
    generate_content_config=_cfg(0.1),
)

meeting_processing = SequentialAgent(
    name="meeting_processing",
    description="Transcript -> insights -> summary, MOM and JIRA plan.",
    sub_agents=[
        insight_extractor,
        ParallelAgent(name="drafters", sub_agents=[summary_writer, mom_writer, jira_planner]),
    ],
)

brd_writer = LlmAgent(
    name="brd_writer",
    model=config.model(),
    description="Writes a BRD by filling in the chosen template.",
    instruction=prompts.brd_from_template_instruction,
    output_key="brd_markdown",
    include_contents="none",
    generate_content_config=_cfg(0.2, capped=True),
    after_agent_callback=_brd_check,
)

LLM_AGENTS = [insight_extractor, summary_writer, mom_writer, jira_planner, brd_writer]


async def run_workflow(agent: BaseAgent, state: dict) -> dict:
    """Runs a workflow agent in a throwaway session seeded with `state`; returns the final state."""
    sessions = InMemorySessionService()
    runner = Runner(app_name="heypm_workflows", agent=agent, session_service=sessions)
    session = await sessions.create_session(app_name="heypm_workflows", user_id="system",
                                            session_id=str(uuid.uuid4()), state=state)
    message = types.Content(role="user", parts=[types.Part(text="Run.")])
    async for _ in runner.run_async(user_id="system", session_id=session.id, new_message=message):
        pass
    final = await sessions.get_session(app_name="heypm_workflows", user_id="system", session_id=session.id)
    return dict(final.state)
