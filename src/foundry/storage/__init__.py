"""Artifact storage: pluggable backends, retention, pinning, archival (docs/81).

Public surface:

- backends: :class:`StorageBackend` protocol, :class:`FilesystemBackend` and
  the cloud backends, :func:`select_backend` env-driven factory.
- retention: :func:`gc`, :func:`archive`, :func:`pin` / :func:`unpin` /
  :func:`list_pinned`, :class:`RetentionPolicy`, :func:`parse_duration`.
- migration: :func:`copy_between` (non-destructive backend-to-backend copy).
- paths: the ``~/.foundry`` layout resolvers (``FOUNDRY_HOME``-aware).
"""

from __future__ import annotations

from foundry.storage.artifacts_store import copy_between
from foundry.storage.backends import (
    AzureBlobBackend,
    FilesystemBackend,
    GCSBackend,
    S3Backend,
    StorageBackend,
    StorageKey,
    StorageMetadata,
    infer_content_type,
    select_backend,
)
from foundry.storage.paths import (
    archives_root,
    foundry_home,
    observability_db_path,
    pinned_global_path,
    run_dir,
    runs_root,
)
from foundry.storage.retention import (
    ArchiveReport,
    GcReport,
    KindRetention,
    PinnedItem,
    RetentionPolicy,
    archive,
    gc,
    list_pinned,
    parse_duration,
    pin,
    unpin,
)

__all__ = [
    "ArchiveReport",
    "AzureBlobBackend",
    "FilesystemBackend",
    "GCSBackend",
    "GcReport",
    "KindRetention",
    "PinnedItem",
    "RetentionPolicy",
    "S3Backend",
    "StorageBackend",
    "StorageKey",
    "StorageMetadata",
    "archive",
    "archives_root",
    "copy_between",
    "foundry_home",
    "gc",
    "infer_content_type",
    "list_pinned",
    "observability_db_path",
    "parse_duration",
    "pin",
    "pinned_global_path",
    "run_dir",
    "runs_root",
    "select_backend",
    "unpin",
]
