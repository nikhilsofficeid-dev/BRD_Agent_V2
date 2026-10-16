"""Fetch a meeting transcript by meeting_id from GCS (or a local folder) and normalise it to plain text.

Lookup order for a meeting_id:
  1. If meeting_id is itself a gs:// URI or an existing file path, read that.
  2. <prefix>/<meeting_id>.txt|.vtt|.json|.md  (first that exists)
  3. <prefix>/<meeting_id>/ (a folder): the first transcript-like file inside it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .. import config

EXTENSIONS = (".txt", ".vtt", ".json", ".md")


class TranscriptNotFound(LookupError):
    pass


@dataclass
class Transcript:
    meeting_id: str
    text: str
    source_uri: str


# ---------------------------------------------------------------------------
# Format normalisation
# ---------------------------------------------------------------------------
_VTT_TIME = re.compile(r"^(\d{2}:)?\d{2}:\d{2}[.,]\d{3}\s+-->\s+")
_VTT_VOICE = re.compile(r"<v\s+([^>]+)>(.*?)(</v>)?$")


def _short_ts(stamp: str) -> str:
    """'00:04:35.120' -> '4:35'; '01:02:03.000' -> '1:02:03'."""
    parts = stamp.split(".")[0].split(",")[0].split(":")
    if len(parts) == 3:
        h, m, s = (int(p) for p in parts)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    m, s = (int(p) for p in parts[-2:])
    return f"{m}:{s:02d}"


def vtt_to_text(raw: str) -> str:
    """Teams/Meet/Zoom WebVTT -> 'Speaker   m:ss\\nutterance' blocks, merging consecutive cues per speaker."""
    lines = raw.replace("\r\n", "\n").split("\n")
    out: list[tuple[str, str, str]] = []
    stamp = ""
    for line in lines:
        line = line.strip()
        if not line or line == "WEBVTT" or line.startswith(("NOTE", "STYLE", "REGION")):
            continue
        if _VTT_TIME.match(line):
            stamp = _short_ts(line.split("-->")[0].strip())
            continue
        if re.fullmatch(r"[0-9a-f\-/]+", line, re.I):  # cue identifiers
            continue
        voice = _VTT_VOICE.match(line)
        if voice:
            speaker, text = voice.group(1).strip(), re.sub(r"<[^>]+>", "", voice.group(2)).strip()
        elif ":" in line and len(line.split(":", 1)[0]) < 40:
            speaker, text = (s.strip() for s in line.split(":", 1))
        else:
            speaker, text = (out[-1][0] if out else "Unknown"), line
        if out and out[-1][0] == speaker:
            out[-1] = (speaker, out[-1][1], f"{out[-1][2]} {text}")
        else:
            out.append((speaker, stamp, text))
    return "\n\n".join(f"{s}   {t}\n{x}" for s, t, x in out)


def json_to_text(raw: str) -> str:
    """Accepts a list of utterances or {'utterances'|'segments'|'entries'|'transcript': [...]}."""
    data = json.loads(raw)
    if isinstance(data, dict):
        for key in ("utterances", "segments", "entries", "transcript", "items"):
            if isinstance(data.get(key), list):
                data = data[key]
                break
            if isinstance(data.get(key), str):
                return data[key]
    if not isinstance(data, list):
        raise ValueError("Unrecognised JSON transcript shape")
    blocks = []
    for u in data:
        if isinstance(u, str):
            blocks.append(u)
            continue
        speaker = u.get("speaker") or u.get("speakerName") or u.get("name") or "Unknown"
        text = u.get("text") or u.get("content") or u.get("transcript") or ""
        start = u.get("start") or u.get("startTime") or u.get("timestamp") or ""
        if isinstance(start, (int, float)):
            start = f"{int(start) // 60}:{int(start) % 60:02d}"
        blocks.append(f"{speaker}   {start}\n{text}".rstrip())
    return "\n\n".join(blocks)


def normalise(raw: str, name: str) -> str:
    lower = name.lower()
    if lower.endswith(".vtt") or raw.lstrip().startswith("WEBVTT"):
        return vtt_to_text(raw)
    if lower.endswith(".json"):
        return json_to_text(raw)
    return raw.strip()


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------
class LocalTranscriptStore:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or (config.local_data_dir() / "transcripts")

    def get(self, meeting_id: str) -> Transcript:
        direct = Path(meeting_id)
        if direct.suffix.lower() in EXTENSIONS and direct.is_file():
            return Transcript(meeting_id, normalise(direct.read_text(encoding="utf-8", errors="replace"), direct.name),
                              str(direct))
        for ext in EXTENSIONS:
            p = self.root / f"{meeting_id}{ext}"
            if p.is_file():
                return Transcript(meeting_id, normalise(p.read_text(encoding="utf-8", errors="replace"), p.name), str(p))
        folder = self.root / meeting_id
        if folder.is_dir():
            for p in sorted(folder.iterdir()):
                if p.suffix.lower() in EXTENSIONS:
                    return Transcript(meeting_id, normalise(p.read_text(encoding="utf-8", errors="replace"), p.name),
                                      str(p))
        raise TranscriptNotFound(f"No transcript for meeting '{meeting_id}' under {self.root}")


class GcsTranscriptStore:
    def __init__(self, bucket: Optional[str] = None, prefix: Optional[str] = None, client=None):
        from google.cloud import storage  # lazy import

        self.client = client or storage.Client(project=config.gcp_project())
        self.bucket_name = bucket or config.transcript_bucket()
        self.prefix = config.transcript_prefix() if prefix is None else prefix
        if not self.bucket_name:
            raise ValueError("TRANSCRIPT_BUCKET is not set")

    def _read(self, bucket: str, name: str) -> Optional[str]:
        blob = self.client.bucket(bucket).blob(name)
        if not blob.exists():
            return None
        return blob.download_as_bytes().decode("utf-8", errors="replace")

    def get(self, meeting_id: str) -> Transcript:
        if meeting_id.startswith("gs://"):
            bucket, _, name = meeting_id[5:].partition("/")
            raw = self._read(bucket, name)
            if raw is None:
                raise TranscriptNotFound(meeting_id)
            return Transcript(meeting_id, normalise(raw, name), meeting_id)
        base = f"{self.prefix}/{meeting_id}" if self.prefix else meeting_id
        for ext in EXTENSIONS:
            raw = self._read(self.bucket_name, base + ext)
            if raw is not None:
                return Transcript(meeting_id, normalise(raw, base + ext), f"gs://{self.bucket_name}/{base}{ext}")
        for blob in self.client.list_blobs(self.bucket_name, prefix=base + "/"):
            if blob.name.lower().endswith(EXTENSIONS):
                raw = blob.download_as_bytes().decode("utf-8", errors="replace")
                return Transcript(meeting_id, normalise(raw, blob.name), f"gs://{self.bucket_name}/{blob.name}")
        raise TranscriptNotFound(f"No transcript for meeting '{meeting_id}' in gs://{self.bucket_name}/{base}[.*|/]")
