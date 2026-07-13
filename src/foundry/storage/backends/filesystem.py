"""Local filesystem backend (docs/81) — dev + single-host prod default.

Keys resolve strictly inside the backend root; anything that escapes after
``Path.resolve()`` (traversal, absolute paths, symlink escapes) is refused
with :class:`~foundry.core.errors.StorageError`. ``content_type`` on ``put``
is ignored — the filesystem has no metadata channel, so content type is
inferred from the key's extension on read (cloud backends store it properly).

File operations are plain synchronous calls: artifacts are small and this
backend is per-host dev infrastructure, not a hot path.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from foundry.core.errors import StorageError, StorageKeyNotFound
from foundry.storage.backends.base import StorageKey, StorageMetadata, infer_content_type
from foundry.storage.paths import foundry_home


class FilesystemBackend:
    """Store objects as files under ``root`` (default: ``foundry_home()``)."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root if root is not None else foundry_home()).resolve()

    @property
    def root(self) -> Path:
        return self._root

    def _resolve(self, key: str) -> Path:
        if not key or key.startswith("/") or "\\" in key:
            raise StorageError(
                f"invalid storage key {key!r}: keys are non-empty, "
                "'/'-separated relative paths",
                context={"key": key},
            )
        candidate = (self._root / key).resolve()
        if candidate == self._root or not candidate.is_relative_to(self._root):
            raise StorageError(
                f"storage key {key!r} escapes the backend root",
                context={"key": key, "root": str(self._root)},
            )
        return candidate

    async def put(
        self, key: str, content: bytes, content_type: str = "application/json"
    ) -> None:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    async def get(self, key: str) -> bytes:
        path = self._resolve(key)
        if not path.is_file():
            raise StorageKeyNotFound(
                f"storage key {key!r} not found",
                context={"key": key, "root": str(self._root)},
            )
        return path.read_bytes()

    async def list(self, prefix: str, limit: int = 1000) -> list[StorageKey]:
        if not self._root.is_dir():
            return []
        results: list[StorageKey] = []
        for key in sorted(
            path.relative_to(self._root).as_posix()
            for path in self._root.rglob("*")
            if path.is_file()
        ):
            if not key.startswith(prefix):
                continue
            results.append(self._describe(self._root / key, key))
            if len(results) >= limit:
                break
        return results

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        if not path.is_file():
            raise StorageKeyNotFound(
                f"storage key {key!r} not found",
                context={"key": key, "root": str(self._root)},
            )
        path.unlink()

    async def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    async def get_metadata(self, key: str) -> StorageMetadata:
        path = self._resolve(key)
        if not path.is_file():
            raise StorageKeyNotFound(
                f"storage key {key!r} not found",
                context={"key": key, "root": str(self._root)},
            )
        stat = path.stat()
        return StorageMetadata(
            key=key,
            size_bytes=stat.st_size,
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            content_type=infer_content_type(key),
        )

    def _describe(self, path: Path, key: str) -> StorageKey:
        stat = path.stat()
        return StorageKey(
            key=key,
            size_bytes=stat.st_size,
            last_modified=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            content_type=infer_content_type(key),
        )


__all__ = ["FilesystemBackend"]
