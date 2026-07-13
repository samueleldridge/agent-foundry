"""Storage backend protocol + shared models (docs/81 § Storage backend abstraction).

Keys are ``/``-separated relative paths (e.g. ``runs/2026/04/<run_id>/trace.jsonl``).
The ``content_type`` passed to :meth:`StorageBackend.put` is honoured by the
cloud backends (S3 / Azure Blob / GCS store it as object metadata); the
filesystem backend has no metadata channel and infers content type from the
key's extension instead (see :func:`infer_content_type`).
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from pydantic import BaseModel, ConfigDict


class StorageKey(BaseModel):
    """One listed object: key + size + mtime + content type."""

    model_config = ConfigDict(extra="forbid")

    key: str
    size_bytes: int
    last_modified: datetime
    content_type: str


class StorageMetadata(BaseModel):
    """Head-style metadata for a single object."""

    model_config = ConfigDict(extra="forbid")

    key: str
    size_bytes: int
    last_modified: datetime
    content_type: str


class StorageBackend(Protocol):
    """Backend for run artifacts, eval results, forge trajectories, archives.

    Audit log + observability.db remain on the local filesystem regardless of
    backend (they are per-host / per-process state; docs/81).
    """

    async def put(
        self, key: str, content: bytes, content_type: str = "application/json"
    ) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def list(self, prefix: str, limit: int = 1000) -> list[StorageKey]: ...

    async def delete(self, key: str) -> None: ...

    async def exists(self, key: str) -> bool: ...

    async def get_metadata(self, key: str) -> StorageMetadata: ...


_EXTENSION_CONTENT_TYPES: dict[str, str] = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".gz": "application/gzip",
}


def infer_content_type(key: str) -> str:
    """Content type from the key's extension; octet-stream when unknown."""
    for suffix, content_type in _EXTENSION_CONTENT_TYPES.items():
        if key.endswith(suffix):
            return content_type
    return "application/octet-stream"


__all__ = [
    "StorageBackend",
    "StorageKey",
    "StorageMetadata",
    "infer_content_type",
]
