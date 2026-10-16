"""People directory: maps names spoken in the call to email and JIRA identities.

Transcripts only contain names, so the agent never guesses an email address or a
JIRA account. Anyone not found here is reported back as unresolved.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import yaml

from . import config


@dataclass
class Person:
    name: str
    email: str
    jira_account_id: Optional[str] = None
    aliases: list[str] = field(default_factory=list)


def load_people() -> list[Person]:
    path = config.people_file()
    if not path.exists():
        return []
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    people = []
    for row in data.get("people", []):
        people.append(
            Person(
                name=row["name"],
                email=row["email"],
                jira_account_id=row.get("jira_account_id"),
                aliases=row.get("aliases", []) or [],
            )
        )
    return people


def _norm(text: str) -> str:
    return " ".join(text.lower().replace(".", " ").split())


def resolve(name: str, people: Optional[list[Person]] = None) -> Optional[Person]:
    """Exact full name, then alias, then a unique first-name match."""
    people = load_people() if people is None else people
    target = _norm(name)
    for p in people:
        if _norm(p.name) == target or target in {_norm(a) for a in p.aliases}:
            return p
    first = target.split(" ")[0] if target else ""
    matches = [p for p in people if _norm(p.name).split(" ")[0] == first]
    return matches[0] if len(matches) == 1 else None


def resolve_many(names: list[str]) -> tuple[list[Person], list[str]]:
    people = load_people()
    found, missing, seen = [], [], set()
    for n in names:
        p = resolve(n, people)
        if p is None:
            missing.append(n)
        elif p.email not in seen:
            seen.add(p.email)
            found.append(p)
    return found, missing
