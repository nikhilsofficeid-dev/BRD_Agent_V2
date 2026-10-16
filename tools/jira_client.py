"""Thin JIRA Cloud REST v3 client covering what the planner can ask for."""
from __future__ import annotations

from typing import Any, Optional

import requests


def adf(text: str) -> dict:
    """Plain text -> Atlassian Document Format (one paragraph per line, '- ' lines as bullets)."""
    content: list[dict] = []
    bullets: list[dict] = []

    def flush():
        nonlocal bullets
        if bullets:
            content.append({"type": "bulletList", "content": bullets})
            bullets = []

    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped.startswith(("- ", "* ")):
            bullets.append({"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": stripped[2:]}]}]})
            continue
        flush()
        if stripped:
            content.append({"type": "paragraph", "content": [{"type": "text", "text": stripped}]})
    flush()
    return {"type": "doc", "version": 1, "content": content or [{"type": "paragraph", "content": []}]}


class JiraError(RuntimeError):
    pass


class JiraClient:
    def __init__(self, base_url: str, email: str, api_token: str, timeout: int = 30):
        if not (base_url and email and api_token):
            raise JiraError("JIRA_BASE_URL, JIRA_EMAIL and JIRA_API_TOKEN must be set.")
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.auth = (email, api_token)
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        self.timeout = timeout

    def _req(self, method: str, path: str, **kwargs) -> Any:
        resp = self.session.request(method, f"{self.base}/rest/api/3{path}", timeout=self.timeout, **kwargs)
        if resp.status_code >= 400:
            raise JiraError(f"{method} {path} -> {resp.status_code}: {resp.text[:500]}")
        return resp.json() if resp.content else None

    # -- reads ---------------------------------------------------------------
    def issue_exists(self, key: str) -> bool:
        try:
            self._req("GET", f"/issue/{key}", params={"fields": "summary"})
            return True
        except JiraError:
            return False

    def find_user(self, email: str) -> Optional[str]:
        users = self._req("GET", "/user/search", params={"query": email}) or []
        return users[0]["accountId"] if users else None

    # -- writes --------------------------------------------------------------
    def create_issue(self, fields: dict) -> str:
        return self._req("POST", "/issue", json={"fields": fields})["key"]

    def assign(self, key: str, account_id: str) -> None:
        self._req("PUT", f"/issue/{key}/assignee", json={"accountId": account_id})

    def comment(self, key: str, text: str) -> None:
        self._req("POST", f"/issue/{key}/comment", json={"body": adf(text)})

    def link(self, link_type: str, issue: str, other_issue: str) -> None:
        # JIRA shows the link type's OUTWARD wording on inwardIssue, e.g. for "Duplicate":
        # inwardIssue=OLD, outwardIssue=NEW renders as "OLD duplicates NEW".
        self._req("POST", "/issueLink", json={
            "type": {"name": link_type},
            "inwardIssue": {"key": issue},
            "outwardIssue": {"key": other_issue},
        })

    def transition(self, key: str, to_status: str, resolution: Optional[str] = None) -> str:
        transitions = self._req("GET", f"/issue/{key}/transitions")["transitions"]
        wanted = to_status.lower()
        synonyms = {wanted} | ({"done", "closed", "resolved"} if wanted in {"done", "closed", "resolved"} else set())
        match = next((t for t in transitions if t["name"].lower() == wanted), None) or next(
            (t for t in transitions if t["to"]["name"].lower() in synonyms or t["name"].lower() in synonyms), None)
        if not match:
            names = ", ".join(t["name"] for t in transitions)
            raise JiraError(f"No transition to '{to_status}' on {key}. Available: {names}")
        body: dict = {"transition": {"id": match["id"]}}
        if resolution:
            try:
                self._req("POST", f"/issue/{key}/transitions",
                          json={**body, "fields": {"resolution": {"name": resolution}}})
                return match["to"]["name"]
            except JiraError:
                pass  # resolution not on the transition screen; transition without it
        self._req("POST", f"/issue/{key}/transitions", json=body)
        return match["to"]["name"]
