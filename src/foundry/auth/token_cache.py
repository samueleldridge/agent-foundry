"""Short-lived token store with expiry-aware refresh (docs/23 § Refresh).

Used by the OAuth / JWT-bearer scheme helpers: derived access tokens are
cached per key and re-fetched when within ``early_refresh_buffer_s`` of
expiry. Long-lived credentials never enter this cache — only tokens derived
from them.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from foundry.core.connection import SecretValue


@dataclass(frozen=True)
class CachedToken:
    value: SecretValue
    expires_at: float | None
    """Unix timestamp; None = no known expiry (cache until evicted)."""


TokenFetcher = Callable[[], Awaitable[tuple[str, float | None]]]
"""Returns (token, expires_at_unix_or_None)."""


class TokenCache:
    """Async-safe token cache with early-refresh semantics."""

    def __init__(self) -> None:
        self._tokens: dict[str, CachedToken] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def peek(self, key: str) -> CachedToken | None:
        return self._tokens.get(key)

    def _is_fresh(self, token: CachedToken, early_refresh_buffer_s: int) -> bool:
        if token.expires_at is None:
            return True
        return time.time() < token.expires_at - early_refresh_buffer_s

    async def get_or_fetch(
        self,
        key: str,
        fetch: TokenFetcher,
        *,
        early_refresh_buffer_s: int = 60,
    ) -> SecretValue:
        """Return a fresh token, fetching (coalesced per key) when the cached
        one is absent or within the early-refresh window of expiry."""
        cached = self._tokens.get(key)
        if cached is not None and self._is_fresh(cached, early_refresh_buffer_s):
            return cached.value
        async with self._lock_for(key):
            cached = self._tokens.get(key)  # re-check under the lock
            if cached is not None and self._is_fresh(cached, early_refresh_buffer_s):
                return cached.value
            raw, expires_at = await fetch()
            token = CachedToken(value=SecretValue(raw), expires_at=expires_at)
            self._tokens[key] = token
            return token.value

    def evict(self, key: str) -> None:
        self._tokens.pop(key, None)

    def clear(self) -> None:
        self._tokens.clear()


__all__ = ["CachedToken", "TokenCache", "TokenFetcher"]
