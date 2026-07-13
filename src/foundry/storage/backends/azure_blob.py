"""Azure Blob Storage backend (docs/81).

``azure-storage-blob`` (and ``azure-identity`` when no connection string is
set) are optional dependencies, imported lazily at construction; a missing
SDK surfaces as :class:`StorageBackendUnavailable` with an install hint.

Auth: ``AZURE_STORAGE_CONNECTION_STRING`` when present, otherwise
``AZURE_STORAGE_ACCOUNT_URL`` + ``DefaultAzureCredential`` (the standard
chain — not foundry's ``SecretsProvider``). The SDK client is synchronous;
calls run on a worker thread via ``asyncio.to_thread``.
"""

from __future__ import annotations

import asyncio
import importlib
import os
from typing import Any

from foundry.core.errors import StorageBackendUnavailable, StorageError, StorageKeyNotFound
from foundry.storage.backends.base import StorageKey, StorageMetadata, infer_content_type


def _load_azure_blob() -> Any:
    try:
        return importlib.import_module("azure.storage.blob")
    except ModuleNotFoundError as exc:
        raise StorageBackendUnavailable(
            "Azure Blob backend requires azure-storage-blob — "
            "`uv pip install azure-storage-blob`",
            context={"package": "azure-storage-blob", "backend": "azure_blob"},
        ) from exc


def _load_azure_identity() -> Any:
    try:
        return importlib.import_module("azure.identity")
    except ModuleNotFoundError as exc:
        raise StorageBackendUnavailable(
            "Azure Blob backend requires azure-identity for credential-chain "
            "auth — `uv pip install azure-identity` (or set "
            "AZURE_STORAGE_CONNECTION_STRING)",
            context={"package": "azure-identity", "backend": "azure_blob"},
        ) from exc


def _is_not_found(exc: Exception) -> bool:
    return (
        type(exc).__name__ == "ResourceNotFoundError"
        or getattr(exc, "status_code", None) == 404
    )


class AzureBlobBackend:
    """Objects live as blobs in one container."""

    def __init__(self, container: str) -> None:
        blob_mod = _load_azure_blob()
        self._blob_mod: Any = blob_mod
        connection_string = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
        if connection_string:
            service: Any = blob_mod.BlobServiceClient.from_connection_string(
                connection_string
            )
        else:
            account_url = os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
            if not account_url:
                raise StorageError(
                    "azure_blob backend needs AZURE_STORAGE_CONNECTION_STRING "
                    "or AZURE_STORAGE_ACCOUNT_URL",
                    context={"backend": "azure_blob"},
                )
            identity = _load_azure_identity()
            service = blob_mod.BlobServiceClient(
                account_url, credential=identity.DefaultAzureCredential()
            )
        self._container: Any = service.get_container_client(container)
        self._container_name = container

    async def put(
        self, key: str, content: bytes, content_type: str = "application/json"
    ) -> None:
        settings = self._blob_mod.ContentSettings(content_type=content_type)
        try:
            await asyncio.to_thread(
                self._container.upload_blob,
                name=key,
                data=content,
                overwrite=True,
                content_settings=settings,
            )
        except Exception as exc:
            raise self._translate(exc, key, "put") from exc

    async def get(self, key: str) -> bytes:
        try:
            downloader = await asyncio.to_thread(self._container.download_blob, key)
            data: bytes = await asyncio.to_thread(downloader.readall)
        except Exception as exc:
            raise self._translate(exc, key, "get") from exc
        return data

    async def list(self, prefix: str, limit: int = 1000) -> list[StorageKey]:
        def _collect() -> list[StorageKey]:
            results: list[StorageKey] = []
            for blob in self._container.list_blobs(name_starts_with=prefix):
                content_settings = getattr(blob, "content_settings", None)
                content_type = getattr(content_settings, "content_type", None)
                results.append(
                    StorageKey(
                        key=blob.name,
                        size_bytes=blob.size,
                        last_modified=blob.last_modified,
                        content_type=str(content_type or infer_content_type(blob.name)),
                    )
                )
                if len(results) >= limit:
                    break
            return results

        try:
            return await asyncio.to_thread(_collect)
        except Exception as exc:
            raise StorageError(
                f"azure_blob list failed for prefix {prefix!r}",
                context={"prefix": prefix, "container": self._container_name},
            ) from exc

    async def delete(self, key: str) -> None:
        try:
            await asyncio.to_thread(self._container.delete_blob, key)
        except Exception as exc:
            raise self._translate(exc, key, "delete") from exc

    async def exists(self, key: str) -> bool:
        try:
            result: bool = await asyncio.to_thread(
                self._container.get_blob_client(key).exists
            )
        except Exception as exc:
            raise self._translate(exc, key, "exists") from exc
        return result

    async def get_metadata(self, key: str) -> StorageMetadata:
        try:
            props = await asyncio.to_thread(
                self._container.get_blob_client(key).get_blob_properties
            )
        except Exception as exc:
            raise self._translate(exc, key, "head") from exc
        content_settings = getattr(props, "content_settings", None)
        content_type = getattr(content_settings, "content_type", None)
        return StorageMetadata(
            key=key,
            size_bytes=props.size,
            last_modified=props.last_modified,
            content_type=str(content_type or infer_content_type(key)),
        )

    def _translate(self, exc: Exception, key: str, op: str) -> StorageError:
        if _is_not_found(exc):
            return StorageKeyNotFound(
                f"storage key {key!r} not found",
                context={"key": key, "container": self._container_name},
            )
        return StorageError(
            f"azure_blob {op} failed for key {key!r}",
            context={"key": key, "container": self._container_name},
        )


__all__ = ["AzureBlobBackend"]
