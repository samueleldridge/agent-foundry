"""Backend-to-backend artifact migration (docs/81 § Storage migrations).

Powers ``foundry storage migrate <from> <to>``: read every key under a
prefix from the source backend and write it to the destination.
Non-destructive — the source is never deleted here (``--delete-source`` is a
separate, opt-in step per docs/81).
"""

from __future__ import annotations

from foundry.storage.backends.base import StorageBackend

# ``StorageBackend.list`` is single-shot (no continuation token), so one
# migration pass copies at most ``limit`` keys; re-run for larger sets.
_DEFAULT_LIMIT = 100_000


async def copy_between(
    src_backend: StorageBackend,
    dst_backend: StorageBackend,
    prefix: str = "",
    *,
    limit: int = _DEFAULT_LIMIT,
) -> list[str]:
    """Copy every object under ``prefix`` from src to dst; returns the copied
    keys in listing order. Content types are carried across."""
    copied: list[str] = []
    for entry in await src_backend.list(prefix, limit=limit):
        content = await src_backend.get(entry.key)
        await dst_backend.put(entry.key, content, content_type=entry.content_type)
        copied.append(entry.key)
    return copied


__all__ = ["copy_between"]
