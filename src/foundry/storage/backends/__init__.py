"""Pluggable storage backends (docs/81 § Storage backend abstraction).

Selection at startup via env vars:

==========================  ============================================
``FOUNDRY_STORAGE_BACKEND``  required companion env vars
==========================  ============================================
unset / ``filesystem``       ``FOUNDRY_STORAGE_ROOT`` (optional override)
``s3``                       ``FOUNDRY_STORAGE_BUCKET`` (+ optional
                             ``FOUNDRY_STORAGE_PREFIX``)
``s3_compatible``            as ``s3`` + ``FOUNDRY_STORAGE_ENDPOINT``
``azure_blob``               ``FOUNDRY_STORAGE_CONTAINER``
``gcs``                      ``FOUNDRY_STORAGE_BUCKET``
==========================  ============================================
"""

from __future__ import annotations

import os
from pathlib import Path

from foundry.core.errors import StorageError
from foundry.storage.backends.azure_blob import AzureBlobBackend
from foundry.storage.backends.base import (
    StorageBackend,
    StorageKey,
    StorageMetadata,
    infer_content_type,
)
from foundry.storage.backends.filesystem import FilesystemBackend
from foundry.storage.backends.gcs import GCSBackend
from foundry.storage.backends.s3 import S3Backend


def _require_env(name: str, backend: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise StorageError(
            f"storage backend {backend!r} requires the {name} env var",
            context={"backend": backend, "missing_env": name},
        )
    return value


def select_backend() -> StorageBackend:
    """Instantiate the backend configured by ``FOUNDRY_STORAGE_BACKEND``."""
    kind = os.environ.get("FOUNDRY_STORAGE_BACKEND", "").strip().lower() or "filesystem"
    if kind == "filesystem":
        root = os.environ.get("FOUNDRY_STORAGE_ROOT", "").strip()
        return FilesystemBackend(Path(root).expanduser() if root else None)
    if kind in ("s3", "s3_compatible"):
        bucket = _require_env("FOUNDRY_STORAGE_BUCKET", kind)
        prefix = os.environ.get("FOUNDRY_STORAGE_PREFIX", "")
        endpoint = (
            _require_env("FOUNDRY_STORAGE_ENDPOINT", kind)
            if kind == "s3_compatible"
            else None
        )
        return S3Backend(bucket=bucket, prefix=prefix, endpoint_url=endpoint)
    if kind == "azure_blob":
        return AzureBlobBackend(container=_require_env("FOUNDRY_STORAGE_CONTAINER", kind))
    if kind == "gcs":
        return GCSBackend(bucket=_require_env("FOUNDRY_STORAGE_BUCKET", kind))
    raise StorageError(
        f"unknown storage backend {kind!r} (expected filesystem, s3, "
        "s3_compatible, azure_blob, or gcs)",
        context={"backend": kind},
    )


__all__ = [
    "AzureBlobBackend",
    "FilesystemBackend",
    "GCSBackend",
    "S3Backend",
    "StorageBackend",
    "StorageKey",
    "StorageMetadata",
    "infer_content_type",
    "select_backend",
]
