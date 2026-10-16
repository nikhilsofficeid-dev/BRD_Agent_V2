"""The per-meeting record in Firestore (or a local JSON file).

Top-level fields are exactly the agreed schema (names kept as specified):

    meeting_id, summary, MOM, jira_actions_requirend, jira_action_description,
    bdr_generated, relevant_jira, user_confirmation_provided,
    last_modified_date, last_modified_time_stamp

Everything else the agent needs to resume work (structured JIRA plan, insights, BRD location,
execution results, status) lives under one nested map, `agent_data`, so the UI contract stays clean.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Optional

from .. import config

SCHEMA_FIELDS = (
    "meeting_id", "summary", "MOM", "jira_actions_requirend", "jira_action_description",
    "bdr_generated", "relevant_jira", "user_confirmation_provided",
    "last_modified_date", "last_modified_time_stamp",
)


def blank_record(meeting_id: str) -> dict:
    return {
        "meeting_id": meeting_id,
        "summary": "",
        "MOM": "",
        "jira_actions_requirend": False,
        "jira_action_description": "",
        "bdr_generated": False,
        "relevant_jira": [],
        "user_confirmation_provided": False,
        "last_modified_date": "",
        "last_modified_time_stamp": "",
        "agent_data": {},
    }


def stamp(record: dict) -> dict:
    now = config.now()
    record["last_modified_date"] = now.date().isoformat()          # e.g. 2026-10-16
    record["last_modified_time_stamp"] = now.isoformat(timespec="seconds")  # e.g. 2026-10-16T09:51:02+05:30
    return record


def public_view(record: dict) -> dict:
    """Only the agreed schema fields (what the UI reads)."""
    return {k: record.get(k) for k in SCHEMA_FIELDS}


def _deep_merge(base: dict, updates: dict) -> dict:
    for k, v in updates.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


class LocalMeetingStore:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or (config.local_data_dir() / "meetings")
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, meeting_id: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in meeting_id)
        return self.root / f"{safe}.json"

    def get(self, meeting_id: str) -> Optional[dict]:
        p = self._path(meeting_id)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def put(self, record: dict) -> dict:
        stamp(record)
        self._path(record["meeting_id"]).write_text(json.dumps(record, indent=2, ensure_ascii=False, default=str),
                                                    encoding="utf-8")
        return record

    def update(self, meeting_id: str, updates: dict) -> dict:
        record = self.get(meeting_id) or blank_record(meeting_id)
        return self.put(_deep_merge(record, copy.deepcopy(updates)))


class FirestoreMeetingStore:
    def __init__(self, client=None, collection: Optional[str] = None):
        if client is None:
            from google.cloud import firestore  # lazy import

            client = firestore.Client(project=config.gcp_project(), database=config.firestore_database())
        self.client = client
        self.collection = collection or config.meetings_collection()

    def _doc(self, meeting_id: str):
        return self.client.collection(self.collection).document(meeting_id)

    def get(self, meeting_id: str) -> Optional[dict]:
        snap = self._doc(meeting_id).get()
        return snap.to_dict() if snap.exists else None

    def put(self, record: dict) -> dict:
        stamp(record)
        self._doc(record["meeting_id"]).set(_json_safe(record))
        return record

    def update(self, meeting_id: str, updates: dict) -> dict:
        record = self.get(meeting_id) or blank_record(meeting_id)
        return self.put(_deep_merge(record, copy.deepcopy(updates)))


def _json_safe(value: Any) -> Any:
    """Firestore rejects some Python types and nested arrays; round-trip through JSON."""
    return json.loads(json.dumps(value, default=str))
