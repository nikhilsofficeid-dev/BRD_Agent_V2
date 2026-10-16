"""JIRA plan: human-readable description, preview, and execution (real or dry-run)."""
from __future__ import annotations

import re
from typing import Any, Optional, Protocol

from .. import config, directory
from .jira_client import JiraClient, JiraError, adf

KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]+-\d+$")


class JiraLike(Protocol):
    def create_issue(self, fields: dict) -> str: ...
    def assign(self, key: str, account_id: str) -> None: ...
    def comment(self, key: str, text: str) -> None: ...
    def link(self, link_type: str, issue: str, other_issue: str) -> None: ...
    def transition(self, key: str, to_status: str, resolution: Optional[str] = None) -> str: ...
    def find_user(self, email: str) -> Optional[str]: ...
    def issue_exists(self, key: str) -> bool: ...


class DryRunJira:
    """Records what would happen; invents placeholder keys for new issues."""

    def __init__(self, project: str):
        self.project, self.n, self.calls = project, 0, []

    def create_issue(self, fields):
        self.n += 1
        key = f"{self.project}-NEW{self.n}"
        self.calls.append(("create", key, fields.get("summary")))
        return key

    def assign(self, key, account_id):
        self.calls.append(("assign", key, account_id))

    def comment(self, key, text):
        self.calls.append(("comment", key))

    def link(self, link_type, issue, other_issue):
        self.calls.append(("link", link_type, issue, other_issue))

    def transition(self, key, to_status, resolution=None):
        self.calls.append(("transition", key, to_status, resolution))
        return to_status

    def find_user(self, email):
        return f"dry-run-account:{email}"

    def issue_exists(self, key):
        return True


# ---------------------------------------------------------------------------
# Plan -> human-readable preview
# ---------------------------------------------------------------------------
def describe(a: dict) -> str:
    act = a["action"]
    if act == "create_issue":
        bits = [f"Create {a.get('issue_type')}: \"{a.get('summary')}\""]
        if a.get("parent"):
            bits.append(f"under {a['parent']}")
        if a.get("assignee"):
            bits.append(f"assignee {a['assignee']}")
        return " ".join(bits)
    if act == "link_issues":
        return f"Link {a.get('issue')} --{a.get('link_type')}--> {a.get('other_issue')}"
    if act == "transition_issue":
        res = f" (resolution {a['resolution']})" if a.get("resolution") else ""
        return f"Move {a.get('issue')} to {a.get('transition_to')}{res}"
    if act == "assign_issue":
        return f"Assign {a.get('issue')} to {a.get('assignee')}"
    if act == "add_comment":
        return f"Comment on {a.get('issue')}: {str(a.get('comment'))[:80]}"
    return act


def preview(plan: dict) -> str:
    lines = [f"Run this JIRA plan on project {plan.get('project_key')}?"
             + ("  [DRY RUN: JIRA will not be touched]" if config.dry_run() else "")]
    for a in plan.get("actions", []):
        ts = f" @{a['source_timestamp']}" if a.get("source_timestamp") else ""
        lines.append(f"  {a['ref']}. {describe(a)}{ts}")
        lines.append(f"      why: {a.get('rationale', '')}")
    if plan.get("notes"):
        lines.append("Not actioned:")
        lines += [f"  - {n}" for n in plan["notes"]]
    lines.append("To skip actions, set payload skip_refs, e.g. [\"A7\", \"A9\"]. Dependent actions are skipped too.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------
def _account_id(name: Optional[str], client: JiraLike, cache: dict) -> Optional[str]:
    if not name:
        return None
    if name in cache:
        return cache[name]
    person = directory.resolve(name)
    acct = None
    if person:
        acct = person.jira_account_id or client.find_user(person.email)
    cache[name] = acct
    return acct


def _description(a: dict, meeting: str) -> str:
    parts = [a.get("description") or ""]
    if a.get("acceptance_criteria"):
        parts.append("Acceptance criteria:")
        parts += [f"- {c}" for c in a["acceptance_criteria"]]
    src = f"Source: {meeting}" + (f", transcript {a['source_timestamp']}" if a.get("source_timestamp") else "")
    parts.append(src)
    return "\n".join(p for p in parts if p)


def run_plan(plan: dict, client: JiraLike, skip_refs: set[str], done: dict, meeting: str) -> dict:
    """Executes actions in order. `done` holds results of a previous partial run (idempotency)."""
    project = plan["project_key"]
    keys: dict[str, str] = {ref: r["key"] for ref, r in done.items() if r.get("key")}
    failed: set[str] = set()
    results: dict[str, Any] = dict(done)
    accounts: dict[str, Optional[str]] = {}

    def resolve_ref(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        if value in keys:
            return keys[value]
        if KEY_RE.match(value):
            return value
        if value.isdigit():
            return f"{project}-{value}"
        raise KeyError(value)

    for a in plan.get("actions", []):
        ref = a["ref"]
        if ref in done and done[ref].get("status") == "ok":
            continue
        deps = [a.get(f) for f in ("parent", "issue", "other_issue") if a.get(f)]
        blocked = [d for d in deps if d in failed or d in skip_refs]
        if ref in skip_refs or blocked:
            results[ref] = {"status": "skipped", "action": describe(a),
                            "reason": "skipped by reviewer" if ref in skip_refs else f"depends on {blocked}"}
            failed.add(ref)
            continue
        try:
            act = a["action"]
            if act == "create_issue":
                fields: dict[str, Any] = {
                    "project": {"key": project},
                    "issuetype": {"name": a.get("issue_type") or "Story"},
                    "summary": a["summary"],
                    "description": adf(_description(a, meeting)),
                    "labels": sorted(set((a.get("labels") or []) + ["meeting-ops"])),
                }
                parent = resolve_ref(a.get("parent"))
                if parent:
                    fields["parent"] = {"key": parent}
                acct = _account_id(a.get("assignee"), client, accounts)
                if acct:
                    fields["assignee"] = {"accountId": acct}
                key = client.create_issue(fields)
                keys[ref] = key
                note = None if acct or not a.get("assignee") else f"assignee '{a['assignee']}' not found; left unassigned"
                results[ref] = {"status": "ok", "action": describe(a), "key": key, **({"note": note} if note else {})}
            elif act == "link_issues":
                client.link(a["link_type"], resolve_ref(a["issue"]), resolve_ref(a["other_issue"]))
                results[ref] = {"status": "ok", "action": describe(a)}
            elif act == "transition_issue":
                status = client.transition(resolve_ref(a["issue"]), a["transition_to"], a.get("resolution"))
                results[ref] = {"status": "ok", "action": describe(a), "now": status}
            elif act == "assign_issue":
                acct = _account_id(a.get("assignee"), client, accounts)
                if not acct:
                    raise JiraError(f"Could not resolve JIRA user for '{a.get('assignee')}'")
                client.assign(resolve_ref(a["issue"]), acct)
                results[ref] = {"status": "ok", "action": describe(a)}
            elif act == "add_comment":
                client.comment(resolve_ref(a["issue"]), a.get("comment") or "")
                results[ref] = {"status": "ok", "action": describe(a)}
            else:
                raise JiraError(f"Unknown action {act}")
        except KeyError as missing:
            failed.add(ref)
            results[ref] = {"status": "error", "action": describe(a), "error": f"unknown ref {missing}"}
        except JiraError as exc:
            failed.add(ref)
            results[ref] = {"status": "error", "action": describe(a), "error": str(exc)}
    return results


def validate_existing_keys(plan: dict, client: JiraLike, skip_refs: set[str] = frozenset()) -> list[str]:
    refs = {a["ref"] for a in plan.get("actions", [])}
    keys = {v for a in plan.get("actions", []) if a["ref"] not in skip_refs
            for f in ("parent", "issue", "other_issue")
            if (v := a.get(f)) and v not in refs and KEY_RE.match(v)}
    return sorted(k for k in keys if not client.issue_exists(k))


def plan_description(plan: dict) -> str:
    """Markdown for the `jira_action_description` field: what will be done in JIRA and why."""
    actions = plan.get("actions") or []
    if not actions:
        return "No JIRA actions were identified in this meeting."
    lines = [f"Proposed JIRA actions for project {plan.get('project_key')}:", ""]
    for a in actions:
        ts = f" ({a['source_timestamp']})" if a.get("source_timestamp") else ""
        lines.append(f"- **{a['ref']}** {describe(a)}{ts}: {a.get('rationale', '')}")
        if a.get("action") == "create_issue":
            if a.get("description"):
                lines.append(f"    - {a['description']}")
            for c in a.get("acceptance_criteria") or []:
                lines.append(f"    - AC: {c}")
    if plan.get("notes"):
        lines += ["", "Not actioned:"] + [f"- {n}" for n in plan["notes"]]
    return "\n".join(lines)


def existing_keys(plan: dict) -> list[str]:
    """Existing JIRA keys the meeting referred to (for `relevant_jira` before anything is created)."""
    project = plan.get("project_key", "")
    refs = {a["ref"] for a in plan.get("actions", [])}
    keys = []
    for a in plan.get("actions", []):
        for f in ("parent", "issue", "other_issue"):
            v = a.get(f)
            if not v or v in refs:
                continue
            key = f"{project}-{v}" if v.isdigit() else v
            if KEY_RE.match(key) and key not in keys:
                keys.append(key)
    return keys


def execute_plan(plan: dict, skip_refs: set[str], done: dict, meeting: str) -> dict:
    """Runs the plan against JIRA (or the dry-run client). Returns {"status", "results", "message"?}."""
    if not plan or not plan.get("actions"):
        return {"status": "skipped", "results": {}, "message": "No JIRA actions were planned."}
    if config.dry_run():
        client: JiraLike = DryRunJira(plan["project_key"])
    else:
        try:
            s = config.jira_settings()
            client = JiraClient(s["base_url"], s["email"], s["api_token"])
        except JiraError as exc:
            return {"status": "error", "results": done, "message": str(exc)}
        missing = validate_existing_keys(plan, client, skip_refs)
        if missing:
            return {"status": "error", "results": done,
                    "message": f"These existing issues were not found or not visible: {missing}. Nothing was changed."}
    results = run_plan(plan, client, skip_refs, dict(done or {}), meeting)
    errors = [r for r in results.values() if r["status"] == "error"]
    status = "dry_run" if config.dry_run() else ("partial" if errors else "ok")
    return {"status": status, "results": results}


def created_keys(results: dict) -> list[str]:
    return [r["key"] for r in (results or {}).values() if r.get("status") == "ok" and r.get("key")]
