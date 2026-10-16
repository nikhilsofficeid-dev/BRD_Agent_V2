"""Backend factories: STORAGE_BACKEND=gcp uses GCS + Firestore; anything else uses local folders."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .. import config
from .files import GcsFileStore, LocalFileStore
from .meetings import FirestoreMeetingStore, LocalMeetingStore, blank_record, public_view
from .templates import Template, TemplateNotFound, TemplateStore
from .transcripts import GcsTranscriptStore, LocalTranscriptStore, Transcript, TranscriptNotFound


@dataclass
class Stores:
    transcripts: Any
    meetings: Any
    templates: TemplateStore
    files: Any


def build_stores() -> Stores:
    if config.storage_backend() == "gcp":
        from google.cloud import firestore, storage

        gcs = storage.Client(project=config.gcp_project())
        fs = firestore.Client(project=config.gcp_project(), database=config.firestore_database())
        return Stores(
            transcripts=GcsTranscriptStore(client=gcs),
            meetings=FirestoreMeetingStore(client=fs),
            templates=TemplateStore(firestore_client=fs, gcs_client=gcs),
            files=GcsFileStore(client=gcs),
        )
    local = config.local_data_dir()
    return Stores(
        transcripts=LocalTranscriptStore(),
        meetings=LocalMeetingStore(),
        templates=TemplateStore(local_dir=local / "templates", source="auto"),
        files=LocalFileStore(),
    )


__all__ = [
    "Stores", "build_stores", "Template", "TemplateNotFound", "TemplateStore", "Transcript",
    "TranscriptNotFound", "blank_record", "public_view",
]
