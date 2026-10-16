"""Where generated files (BRD .docx / .md) are written: GCS or a local folder."""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .. import config


class LocalFileStore:
    def __init__(self, root: Optional[Path] = None):
        self.root = root or (config.local_data_dir() / "outputs")

    def save(self, relative_path: str, data: bytes, content_type: str) -> str:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)


class GcsFileStore:
    def __init__(self, bucket: Optional[str] = None, prefix: Optional[str] = None, client=None):
        from google.cloud import storage  # lazy import

        self.client = client or storage.Client(project=config.gcp_project())
        self.bucket_name = bucket or config.output_bucket()
        self.prefix = config.output_prefix() if prefix is None else prefix
        if not self.bucket_name:
            raise ValueError("OUTPUT_BUCKET (or TRANSCRIPT_BUCKET) is not set")

    def save(self, relative_path: str, data: bytes, content_type: str) -> str:
        name = f"{self.prefix}/{relative_path}" if self.prefix else relative_path
        self.client.bucket(self.bucket_name).blob(name).upload_from_string(data, content_type=content_type)
        return f"gs://{self.bucket_name}/{name}"
