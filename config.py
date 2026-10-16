"""Settings, read lazily from the environment (BRD_Agent_V2/.env)."""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

PACKAGE_DIR = Path(__file__).resolve().parent


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _path(name: str, default: Path) -> Path:
    value = os.getenv(name)
    path = Path(value) if value else default
    return path if path.is_absolute() else PACKAGE_DIR / path


# --- Model ------------------------------------------------------------------
def model() -> str:
    return os.getenv("GEMINI_MODEL", "gemini-2.5-flash")


# --- Backends -----------------------------------------------------------------
def storage_backend() -> str:
    """'gcp' (GCS + Firestore) or 'local' (folders + JSON files, for development)."""
    return os.getenv("STORAGE_BACKEND", "local").strip().lower()


def gcp_project() -> str | None:
    return os.getenv("GOOGLE_CLOUD_PROJECT") or None


# Transcripts: gs://TRANSCRIPT_BUCKET/TRANSCRIPT_PREFIX/<meeting_id>.(txt|vtt|json|md)
def transcript_bucket() -> str:
    return os.getenv("TRANSCRIPT_BUCKET", "")


def transcript_prefix() -> str:
    return os.getenv("TRANSCRIPT_PREFIX", "transcripts").strip("/")


# Templates: GCS folder and/or a Firestore collection; see stores/templates.py
def template_source() -> str:
    """'auto' (Firestore, then GCS, then default), 'gcs', 'firestore', or 'default'."""
    return os.getenv("TEMPLATE_SOURCE", "auto").strip().lower()


def template_bucket() -> str:
    return os.getenv("TEMPLATE_BUCKET", "") or transcript_bucket()


def template_prefix() -> str:
    return os.getenv("TEMPLATE_PREFIX", "templates/brd").strip("/")


def templates_collection() -> str:
    return os.getenv("FIRESTORE_TEMPLATES_COLLECTION", "brd_templates")


# Generated files (BRD .docx/.md): gs://OUTPUT_BUCKET/OUTPUT_PREFIX/<meeting_id>/...
def output_bucket() -> str:
    return os.getenv("OUTPUT_BUCKET", "") or transcript_bucket()


def output_prefix() -> str:
    return os.getenv("OUTPUT_PREFIX", "brds").strip("/")


# Firestore
def firestore_database() -> str:
    return os.getenv("FIRESTORE_DATABASE", "(default)")


def meetings_collection() -> str:
    return os.getenv("FIRESTORE_MEETINGS_COLLECTION", "meetings")


# Local backend folders
def local_data_dir() -> Path:
    return _path("LOCAL_DATA_DIR", PACKAGE_DIR / "local_data")


def default_template_path() -> Path:
    return PACKAGE_DIR / "templates" / "default_brd_template.md"


def people_file() -> Path:
    return _path("PEOPLE_FILE", PACKAGE_DIR / "people.yaml")


# --- Actions -------------------------------------------------------------------
def dry_run() -> bool:
    """When true (the default), nothing is emailed and JIRA is not touched."""
    return _bool("DRY_RUN", True)


def jira_project_key() -> str:
    return os.getenv("JIRA_PROJECT_KEY", "FINAUTO")


def jira_settings() -> dict:
    return {
        "base_url": os.getenv("JIRA_BASE_URL", "").rstrip("/"),
        "email": os.getenv("JIRA_EMAIL", ""),
        "api_token": os.getenv("JIRA_API_TOKEN", ""),
    }


def smtp_settings() -> dict:
    return {
        "host": os.getenv("SMTP_HOST", ""),
        "port": int(os.getenv("SMTP_PORT", "587")),
        "username": os.getenv("SMTP_USERNAME", ""),
        "password": os.getenv("SMTP_PASSWORD", ""),
        "use_starttls": _bool("SMTP_STARTTLS", True),
        "sender": os.getenv("MAIL_FROM", ""),
    }


# --- Time ------------------------------------------------------------------------
def timezone() -> ZoneInfo:
    return ZoneInfo(os.getenv("APP_TIMEZONE", "Asia/Kolkata"))


def now() -> datetime:
    return datetime.now(timezone())
