"""Structured outputs passed between agents via session state."""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Stage 1: what the extractor pulls out of the raw transcript
# ---------------------------------------------------------------------------
class Attendee(BaseModel):
    name: str
    role: Optional[str] = Field(None, description="Role as stated or clearly implied in the call")
    joined_late: bool = False


class Requirement(BaseModel):
    id: str = Field(description="FR-01, NFR-01, ...")
    kind: Literal["functional", "non_functional"]
    description: str
    priority: Literal["must", "should", "could", "wont"] = "must"
    source_timestamp: Optional[str] = Field(None, description="Transcript timestamp, e.g. 4:35")


class BusinessRule(BaseModel):
    rule: str
    source_timestamp: Optional[str] = None


class Decision(BaseModel):
    decision: str
    decided_by: Optional[str] = None
    source_timestamp: Optional[str] = None


class ActionItem(BaseModel):
    description: str
    owner: str = Field(description="Person name exactly as in the transcript, or 'Unassigned'")
    due: Optional[str] = Field(None, description="Due date or phrase as said, e.g. 'today', 'by Monday'")
    source_timestamp: Optional[str] = None


class OpenQuestion(BaseModel):
    question: str
    owner: Optional[str] = None
    source_timestamp: Optional[str] = None


class MeetingInsights(BaseModel):
    meeting_title: str
    meeting_date: Optional[str] = Field(None, description="ISO date if determinable")
    facilitator: Optional[str] = None
    attendees: List[Attendee]
    purpose: str
    problem_statement: str
    current_state: str
    discussion_summary: List[str] = Field(description="Key discussion points, one line each")
    requirements: List[Requirement]
    business_rules: List[BusinessRule]
    decisions: List[Decision]
    out_of_scope: List[str]
    dependencies: List[str]
    risks: List[str]
    success_metrics: List[str]
    timeline: Optional[str] = None
    action_items: List[ActionItem]
    open_questions: List[OpenQuestion]
    mom_extra_cc: List[str] = Field(
        default_factory=list,
        description="People explicitly asked to be copied on the MOM who are not attendees",
    )
    jira_requests: List[str] = Field(
        default_factory=list,
        description="Every explicit JIRA instruction from the call, paraphrased with its timestamp",
    )


# ---------------------------------------------------------------------------
# Stage 2: the JIRA plan (executed only after human approval)
# ---------------------------------------------------------------------------
class JiraAction(BaseModel):
    ref: str = Field(description="Unique temp id for this action, e.g. A1, A2")
    action: Literal["create_issue", "link_issues", "transition_issue", "assign_issue", "add_comment"]
    rationale: str = Field(description="Why, citing the transcript")
    source_timestamp: Optional[str] = None

    # create_issue
    issue_type: Optional[Literal["Epic", "Story", "Task", "Sub-task", "Bug"]] = None
    summary: Optional[str] = None
    description: Optional[str] = None
    acceptance_criteria: List[str] = Field(default_factory=list)
    parent: Optional[str] = Field(None, description="Existing key (FINAUTO-12) or ref of a created issue (A1)")
    labels: List[str] = Field(default_factory=list)

    # create_issue / assign_issue
    assignee: Optional[str] = Field(None, description="Person name as in the transcript")

    # link / transition / assign / comment target
    issue: Optional[str] = Field(None, description="Existing key or ref of a created issue")

    # link_issues
    link_type: Optional[Literal["Relates", "Duplicate", "Blocks"]] = None
    other_issue: Optional[str] = Field(
        None,
        description="Second issue. For Duplicate, 'issue' is the duplicate and 'other_issue' the one it duplicates",
    )

    # transition_issue
    transition_to: Optional[str] = Field(None, description="Target status, e.g. Done, Closed")
    resolution: Optional[str] = Field(None, description="e.g. Duplicate, Done")

    # add_comment
    comment: Optional[str] = None


class JiraPlan(BaseModel):
    project_key: str
    actions: List[JiraAction]
    notes: List[str] = Field(default_factory=list, description="Things deliberately not actioned, and why")
