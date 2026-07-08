"""Factory for pgvector@v1: asyncpg pool; health verifies the extension."""

import time

from foundry.core.connection import (
    ConnectionContext,
    ConnectionHealth,
    ResolvedConnectionCredentials,
)
from foundry.core.errors import ConnectionConfigError


class PgVectorConnection:
    def __init__(self, ref: str, pool, dimensions: int) -> None:
        self.ref = ref
        self.slot = ""
        self._pool = pool
        self.dimensions = dimensions

    @property
    def client(self):
        return self._pool  # asyncpg.Pool

    async def health(self) -> ConnectionHealth:
        from datetime import UTC, datetime

        started = time.monotonic()
        try:
            async with self._pool.acquire() as conn:
                version = await conn.fetchval(
                    "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
                )
            ok = version is not None
            message = (
                f"pgvector {version}" if ok else "pgvector extension not installed"
            )
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
    config,  # PgVectorConfig instance
    credentials: ResolvedConnectionCredentials,
    ctx: ConnectionContext,
) -> PgVectorConnection:
    try:
        import asyncpg  # noqa: PLC0415 -- optional dependency, lazy on purpose
    except ImportError as exc:
        raise ConnectionConfigError(
            "catalog/pgvector requires the optional 'asyncpg' package "
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
    return PgVectorConnection(
        "catalog/pgvector@v1", pool, config.embedding_dimensions
    )
