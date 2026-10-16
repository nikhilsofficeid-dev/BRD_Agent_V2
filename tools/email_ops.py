"""Build and send the MOM email (recipients resolved from people.yaml, never guessed)."""
from __future__ import annotations

import re
import smtplib
import ssl
from email.message import EmailMessage
from pathlib import Path

from .. import config, directory


def _to_html(markdown_text: str) -> str | None:
    try:
        import markdown  # type: ignore
    except ImportError:
        return None
    body = markdown.markdown(markdown_text, extensions=["tables"])
    return (
        "<html><body style='font-family:Segoe UI,Arial,sans-serif;font-size:14px'>"
        "<style>table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:4px 8px}</style>"
        f"{body}</body></html>"
    )


def build_email(insights: dict, mom: str) -> dict:
    """Works out subject, recipients and body deterministically (no LLM guessing)."""
    insights = insights or {}
    title = insights.get("meeting_title", "Meeting")
    date = insights.get("meeting_date") or ""
    subject = f"MOM: {title}" + (f" | {date}" if date else "")

    to_names = [a["name"] for a in insights.get("attendees", [])]
    cc_names = list(insights.get("mom_extra_cc", []))
    to_people, missing_to = directory.resolve_many(to_names)
    cc_people, missing_cc = directory.resolve_many(cc_names)
    to_emails = [p.email for p in to_people]
    cc_emails = [p.email for p in cc_people if p.email not in to_emails]

    body = (mom or "").strip()
    body = re.sub(r"^```[a-zA-Z]*\n|\n```$", "", body)
    return {
        "subject": subject,
        "to": to_emails,
        "cc": cc_emails,
        "unresolved": missing_to + missing_cc,
        "body": body,
    }


def _smtp_send(mail: dict, attach: str | None) -> None:
    s = config.smtp_settings()
    if not (s["host"] and s["sender"]):
        raise RuntimeError("SMTP_HOST and MAIL_FROM must be set to send email.")
    msg = EmailMessage()
    msg["Subject"] = mail["subject"]
    msg["From"] = s["sender"]
    msg["To"] = ", ".join(mail["to"])
    if mail["cc"]:
        msg["Cc"] = ", ".join(mail["cc"])
    msg.set_content(mail["body"])
    html = _to_html(mail["body"])
    if html:
        msg.add_alternative(html, subtype="html")
    if attach and Path(attach).exists():
        msg.add_attachment(
            Path(attach).read_bytes(),
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
            filename=Path(attach).name,
        )
    with smtplib.SMTP(s["host"], s["port"], timeout=30) as server:
        if s["use_starttls"]:
            server.starttls(context=ssl.create_default_context())
        if s["username"]:
            server.login(s["username"], s["password"])
        server.send_message(msg)


def send_mom(insights: dict, mom: str, overrides: dict | None = None, attach: str | None = None) -> dict:
    """Sends (or dry-runs) the MOM. `overrides` may replace subject/to/cc."""
    mail = build_email(insights, mom)
    for key in ("subject", "to", "cc"):
        if overrides and overrides.get(key):
            mail[key] = overrides[key]
    if not mail["body"]:
        return {"status": "error", "message": "MOM is empty."}
    if not mail["to"]:
        return {"status": "error", "message": "No resolvable recipients. Add attendees to people.yaml.",
                "unresolved": mail["unresolved"]}
    if config.dry_run():
        result = {"status": "dry_run", "would_send_to": mail["to"], "cc": mail["cc"], "subject": mail["subject"]}
    else:
        try:
            _smtp_send(mail, attach)
        except Exception as exc:
            return {"status": "error", "message": f"SMTP send failed: {exc}"}
        result = {"status": "sent", "to": mail["to"], "cc": mail["cc"], "subject": mail["subject"]}
    result["unresolved"] = mail["unresolved"]
    return result
