"""Command-line driver for BRD_Agent_V2 (handy before the API exists). Run from inside BRD_Agent_V2:

    python run.py process  MTG-2026-10-15-INVOICE-ALERTS            # transcript -> Firestore record
    python run.py show     MTG-2026-10-15-INVOICE-ALERTS            # print the record (UI fields)
    python run.py ask      MTG-2026-10-15-INVOICE-ALERTS "What did Anita say about SLAs?"
    python run.py confirm  MTG-2026-10-15-INVOICE-ALERTS --skip A3,A4 [--no-email]
    python run.py templates
    python run.py brd      MTG-2026-10-15-INVOICE-ALERTS [--template "Finance BRD"]
    python run.py chat     MTG-2026-10-15-INVOICE-ALERTS            # interactive chat
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from dotenv import load_dotenv

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # so "BRD_Agent_V2" imports as a package
load_dotenv(HERE / ".env")

from BRD_Agent_V2.service import get_service  # noqa: E402


def _print(obj) -> None:
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


async def _chat(svc, meeting_id: str) -> None:
    session_id = None
    print("Ask about the meeting (empty line to quit).")
    while True:
        q = input("\nPM> ").strip()
        if not q:
            return
        r = await svc.ask(meeting_id, q, session_id=session_id)
        session_id = r["session_id"]
        while r.get("pending_confirmation"):
            p = r["pending_confirmation"]
            print("\n" + (p.get("hint") or ""))
            ok = input("\nConfirm? [y/N] ").strip().lower() in {"y", "yes"}
            skips = []
            if ok:
                raw = input("Refs to skip (comma-separated, Enter for none): ").strip()
                skips = [s.strip() for s in raw.split(",") if s.strip()]
            r = await svc.answer_confirmation(session_id, p["confirmation_id"], ok, {"skip_refs": skips})
        print(f"\nHeyPM> {r['answer']}")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["process", "show", "ask", "confirm", "templates", "brd", "chat"])
    parser.add_argument("meeting_id", nargs="?")
    parser.add_argument("question", nargs="?")
    parser.add_argument("--force", action="store_true", help="process: redo even if already confirmed")
    parser.add_argument("--skip", default="", help="confirm: comma-separated plan refs to skip")
    parser.add_argument("--no-email", action="store_true", help="confirm: don't email the MOM")
    parser.add_argument("--template", default=None, help="brd: template id or name")
    parser.add_argument("--full", action="store_true", help="show: include agent_data")
    a = parser.parse_args()
    svc = get_service()

    if a.command == "templates":
        return _print(svc.list_templates())
    if not a.meeting_id:
        parser.error("meeting_id is required")
    if a.command == "process":
        _print(await svc.process_meeting(a.meeting_id, force=a.force))
    elif a.command == "show":
        _print(svc.get_record(a.meeting_id, include_agent_data=a.full))
    elif a.command == "ask":
        r = await svc.ask(a.meeting_id, a.question or "Summarise this meeting in three bullets.")
        print(r["answer"])
    elif a.command == "confirm":
        skips = [s.strip() for s in a.skip.split(",") if s.strip()]
        rec = await svc.confirm_actions(a.meeting_id, skip_refs=skips, send_email=not a.no_email)
        ad = rec["agent_data"]
        _print({"user_confirmation_provided": rec["user_confirmation_provided"],
                "relevant_jira": rec["relevant_jira"], "jira_status": ad.get("jira_status"),
                "jira_message": ad.get("jira_message"), "jira_results": ad.get("jira_results"),
                "mom_email": ad.get("mom_email")})
    elif a.command == "brd":
        rec = await svc.generate_brd(a.meeting_id, template_id=a.template)
        _print({"bdr_generated": rec["bdr_generated"], **rec["agent_data"]["brd"]})
    elif a.command == "chat":
        await _chat(svc, a.meeting_id)


if __name__ == "__main__":
    asyncio.run(main())
