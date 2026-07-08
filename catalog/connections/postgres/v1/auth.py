"""Factory for postgres@v1: asyncpg pool with basic (user/pass) auth.

asyncpg is imported lazily so the catalog stays loadable without the
optional dependency; building the connection without it raises a
structured ConnectionConfigError.
"""

import time

from foundry.core.connection import (
    ConnectionContext,
    ConnectionHealth,
    ResolvedConnectionCredentials,
)
from foundry.core.errors import ConnectionConfigError


class PostgresConnection:
    def __init__(self, ref: str, pool) -> None:
        self.ref = ref
        self.slot = ""
        self._pool = pool

    @property
    def client(self):
        return self._pool  # asyncpg.Pool

    async def health(self) -> ConnectionHealth:
        from datetime import UTC, datetime

        started = time.monotonic()
        try:
            async with self._pool.acquire() as conn:
                value = await conn.fetchval("SELECT 1")
            ok = value == 1
            message = "SELECT 1 ok" if ok else f"SELECT 1 returned {value!r}"
        except Exception as exc:
            ok = False
            message = f"{type(exc).__name__}: {exc}"
        return ConnectionHealth(
            ok=ok,
            latency_ms=int((time.monotonic() - started) * 1000),
            message=message,
            checked_at=datetime.now(UTC),
        )

    async def close(self) -> None:
        await self._pool.close()


async def build_connection(
    config,  # PostgresConfig instance
    credentials: ResolvedConnectionCredentials,
    ctx: ConnectionContext,
) -> PostgresConnection:
    try:
        import asyncpg  # noqa: PLC0415 -- optional dependency, lazy on purpose
    except ImportError as exc:
        raise ConnectionConfigError(
            "catalog/postgres requires the optional 'asyncpg' package "
            "(uv add asyncpg) — not pinned by the framework in Phase 2a",
            context={"missing_package": "asyncpg"},
            cause=exc,
        ) from exc

    pool = await asyncpg.create_pool(
        host=config.host,
        port=config.port,
        database=config.database,
        user=credentials.require("username").reveal(),
        password=credentials.require("password").reveal(),
        min_size=config.min_pool_size,
        max_size=config.max_pool_size,
        timeout=config.connect_timeout_s,
    )
    return PostgresConnection("catalog/postgres@v1", pool)
