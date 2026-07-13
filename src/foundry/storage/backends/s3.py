"""S3 / S3-compatible backend (docs/81) — MinIO and R2 via ``endpoint_url``.

boto3 is an optional dependency: it is imported lazily at construction and a
missing SDK surfaces as :class:`StorageBackendUnavailable` with an install
hint. Auth follows boto3's standard credential chain, not foundry's
``SecretsProvider`` (storage backends are infrastructure, not connections).

The boto3 client is synchronous; calls are pushed onto a worker thread with
``asyncio.to_thread`` to satisfy the async protocol without blocking the loop.
"""

from __future__ import annotations

import asyncio
import importlib
from typing import Any

from foundry.core.errors import StorageBackendUnavailable, StorageError, StorageKeyNotFound
from foundry.storage.backends.base import StorageKey, StorageMetadata, infer_content_type

_NOT_FOUND_CODES = {"404", "NoSuchKey", "NotFound"}


def _load_boto3() -> Any:
    try:
        return importlib.import_module("boto3")
    except ModuleNotFoundError as exc:
        raise StorageBackendUnavailable(
            "S3 backend requires boto3 — `uv pip install boto3`",
            context={"package": "boto3", "backend": "s3"},
        ) from exc


def _error_code(exc: Exception) -> str:
    """Extract the botocore ClientError code without importing botocore."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error")
        if isinstance(error, dict):
            return str(error.get("Code", ""))
    return ""


class S3Backend:
    """Objects live at ``s3://<bucket>/<prefix>/<key>``."""

    def __init__(
        self, bucket: str, prefix: str = "", endpoint_url: str | None = None
    ) -> None:
        boto3 = _load_boto3()
        self._bucket = bucket
        self._prefix = prefix.strip("/")
        self._client: Any = boto3.client("s3", endpoint_url=endpoint_url)

    def _full(self, key: str) -> str:
        return f"{self._prefix}/{key}" if self._prefix else key

    def _bare(self, full_key: str) -> str:
        return full_key[len(self._prefix) + 1 :] if self._prefix else full_key

    async def put(
        self, key: str, content: bytes, content_type: str = "application/json"
    ) -> None:
        try:
            await asyncio.to_thread(
                self._client.put_object,
                Bucket=self._bucket,
                Key=self._full(key),
                Body=content,
                ContentType=content_type,
            )
        except Exception as exc:
            raise StorageError(
                f"s3 put failed for key {key!r}", context={"key": key}
            ) from exc

    async def get(self, key: str) -> bytes:
        try:
            response = await asyncio.to_thread(
                self._client.get_object, Bucket=self._bucket, Key=self._full(key)
            )
            data: bytes = await asyncio.to_thread(response["Body"].read)
        except Exception as exc:
            raise self._translate(exc, key, "get") from exc
        return data

    async def list(self, prefix: str, limit: int = 1000) -> list[StorageKey]:
        results: list[StorageKey] = []
        token: str | None = None
        try:
            while len(results) < limit:
                kwargs: dict[str, Any] = {
                    "Bucket": self._bucket,
                    "Prefix": self._full(prefix),
                    "MaxKeys": min(1000, limit - len(results)),
                }
                if token is not None:
                    kwargs["ContinuationToken"] = token
                page = await asyncio.to_thread(self._client.list_objects_v2, **kwargs)
                for obj in page.get("Contents", []):
                    key = self._bare(str(obj["Key"]))
                    results.append(
                        StorageKey(
                            key=key,
                            size_bytes=obj["Size"],
                            last_modified=obj["LastModified"],
                            content_type=infer_content_type(key),
                        )
                    )
                if not page.get("IsTruncated"):
                    break
                token = str(page.get("NextContinuationToken", "")) or None
        except Exception as exc:
            raise StorageError(
                f"s3 list failed for prefix {prefix!r}", context={"prefix": prefix}
            ) from exc
        return results[:limit]

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(
                self._client.delete_object, Bucket=self._bucket, Key=self._full(key)
            )
        except Exception as exc:
            raise self._translate(exc, key, "delete") from exc

    async def exists(self, key: str) -> bool:
        try:
            await asyncio.to_thread(
                self._client.head_object, Bucket=self._bucket, Key=self._full(key)
            )
        except Exception as exc:
            if _error_code(exc) in _NOT_FOUND_CODES:
                return False
            raise self._translate(exc, key, "head") from exc
        return True

    async def get_metadata(self, key: str) -> StorageMetadata:
        try:
            head = await asyncio.to_thread(
                self._client.head_object, Bucket=self._bucket, Key=self._full(key)
            )
        except Exception as exc:
            raise self._translate(exc, key, "head") from exc
        return StorageMetadata(
            key=key,
            size_bytes=head["ContentLength"],
            last_modified=head["LastModified"],
            content_type=str(head.get("ContentType") or infer_content_type(key)),
        )

    def _translate(self, exc: Exception, key: str, op: str) -> StorageError:
        if _error_code(exc) in _NOT_FOUND_CODES:
            return StorageKeyNotFound(
                f"storage key {key!r} not found",
                context={"key": key, "bucket": self._bucket},
            )
        return StorageError(
            f"s3 {op} failed for key {key!r}",
            context={"key": key, "bucket": self._bucket},
        )


__all__ = ["S3Backend"]
