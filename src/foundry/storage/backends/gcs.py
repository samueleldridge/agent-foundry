"""Google Cloud Storage backend (docs/81).

``google-cloud-storage`` is an optional dependency, imported lazily at
construction; a missing SDK surfaces as :class:`StorageBackendUnavailable`
with an install hint. Auth follows Application Default Credentials. The SDK
client is synchronous; calls run on a worker thread via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from foundry.core.errors import StorageBackendUnavailable, StorageError, StorageKeyNotFound
from foundry.storage.backends.base import StorageKey, StorageMetadata, infer_content_type


def _load_gcs() -> Any:
    try:
        return importlib.import_module("google.cloud.storage")
    except ModuleNotFoundError as exc:
        raise StorageBackendUnavailable(
            "GCS backend requires google-cloud-storage — "
            "`uv pip install google-cloud-storage`",
            context={"package": "google-cloud-storage", "backend": "gcs"},
        ) from exc


def _is_not_found(exc: Exception) -> bool:
    return type(exc).__name__ == "NotFound" or getattr(exc, "code", None) == 404


class GCSBackend:
    """Objects live as blobs in one bucket."""

    def __init__(self, bucket: str) -> None:
        storage_mod = _load_gcs()
        self._client: Any = storage_mod.Client()
        self._bucket: Any = self._client.bucket(bucket)
        self._bucket_name = bucket

    async def put(
        self, key: str, content: bytes, content_type: str = "application/json"
    ) -> None:
        blob = self._bucket.blob(key)
        try:
            await asyncio.to_thread(
                blob.upload_from_string, content, content_type=content_type
            )
        except Exception as exc:
            raise self._translate(exc, key, "put") from exc

    async def get(self, key: str) -> bytes:
        blob = self._bucket.blob(key)
        try:
            data: bytes = await asyncio.to_thread(blob.download_as_bytes)
        except Exception as exc:
            raise self._translate(exc, key, "get") from exc
        return data

    async def list(self, prefix: str, limit: int = 1000) -> list[StorageKey]:
        def _collect() -> list[StorageKey]:
            results: list[StorageKey] = []
            blobs = self._client.list_blobs(
                self._bucket_name, prefix=prefix, max_results=limit
            )
            for blob in blobs:
                results.append(
                    StorageKey(
                        key=blob.name,
                        size_bytes=blob.size,
                        last_modified=blob.updated,
                        content_type=str(
                            blob.content_type or infer_content_type(blob.name)
                        ),
                    )
                )
            return results

        try:
            return await asyncio.to_thread(_collect)
        except Exception as exc:
            raise StorageError(
                f"gcs list failed for prefix {prefix!r}",
                context={"prefix": prefix, "bucket": self._bucket_name},
            ) from exc

    async def delete(self, key: str) -> None:
        blob = self._bucket.blob(key)
        try:
            await asyncio.to_thread(blob.delete)
        except Exception as exc:
            raise self._translate(exc, key, "delete") from exc

    async def exists(self, key: str) -> bool:
        blob = self._bucket.blob(key)
        try:
            result: bool = await asyncio.to_thread(blob.exists)
        except Exception as exc:
            raise self._translate(exc, key, "exists") from exc
        return result

    async def get_metadata(self, key: str) -> StorageMetadata:
        blob = self._bucket.blob(key)
        try:
            await asyncio.to_thread(blob.reload)
        except Exception as exc:
            raise self._translate(exc, key, "head") from exc
        return StorageMetadata(
            key=key,
            size_bytes=blob.size,
            last_modified=blob.updated,
            content_type=str(blob.content_type or infer_content_type(key)),
        )

    def _translate(self, exc: Exception, key: str, op: str) -> StorageError:
        if _is_not_found(exc):
            return StorageKeyNotFound(
                f"storage key {key!r} not found",
                context={"key": key, "bucket": self._bucket_name},
            )
        return StorageError(
            f"gcs {op} failed for key {key!r}",
            context={"key": key, "bucket": self._bucket_name},
        )


__all__ = ["GCSBackend"]
