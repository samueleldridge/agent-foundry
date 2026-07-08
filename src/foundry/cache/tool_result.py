"""ResultCache backends: exact-match caches for idempotent tool outputs
(docs/24 § Layer 3).

Simpler than semantic caching — no similarity search, no embedder, no
threshold. Backends share storage with the semantic cache where possible:
in_process (SQLite), redis (SETEX), postgres (same DB, plain table, no
vector index). The ``input_hash`` argument is the SCOPED key the dispatcher
builds via ``foundry.core.cache.scoped_input_hash``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from foundry.core import CachedToolResult
from foundry.core.errors import CacheBackendError


class InProcessResultCache:
    """SQLite-backed exact-match store — the default (and only config-free)
    backend in 2b."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._conn: sqlite3.Connection | None = None

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        try:
            if self._path != ":memory:":
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS tool_results (
                    tool_ref TEXT NOT NULL,
                    tool_version TEXT NOT NULL,
                    input_hash TEXT NOT NULL,
                    output TEXT NOT NULL,
                    cached_at TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    PRIMARY KEY (tool_ref, tool_version, input_hash)
                )
                """
            )
            conn.commit()
        except (sqlite3.Error, OSError) as exc:
            raise CacheBackendError(
                f"in_process tool-result cache unavailable at "
                f"{self._path!r}: {exc}",
                context={"backend": "in_process", "path": self._path},
                cause=exc,
            ) from exc
        self._conn = conn
        return conn

    async def lookup(
        self, tool_ref: str, tool_version: str, input_hash: str
    ) -> CachedToolResult | None:
        conn = self._connect()
        try:
            conn.execute(
                "DELETE FROM tool_results WHERE expires_at <= ?", (time.time(),)
            )
            row = conn.execute(
                "SELECT output, cached_at FROM tool_results WHERE tool_ref = ? "
                "AND tool_version = ? AND input_hash = ?",
                (tool_ref, tool_version, input_hash),
            ).fetchone()
            conn.commit()
        except sqlite3.Error as exc:
            raise CacheBackendError(
                f"in_process tool-result cache lookup failed: {exc}",
                context={"backend": "in_process", "path": self._path},
                cause=exc,
            ) from exc
        if row is None:
            return None
        try:
            output = json.loads(row[0])
        except json.JSONDecodeError as exc:
            raise CacheBackendError(
                "in_process tool-result cache entry is corrupted",
                context={"backend": "in_process", "tool_ref": tool_ref},
                cause=exc,
            ) from exc
        return CachedToolResult(
            output=output, cached_at=datetime.fromisoformat(row[1])
        )

    async def store(
        self,
        tool_ref: str,
        tool_version: str,
        input_hash: str,
        output: BaseModel,
        ttl_s: int,
    ) -> None:
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO tool_results (tool_ref, tool_version, input_hash, "
                "output, cached_at, expires_at) VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT (tool_ref, tool_version, input_hash) DO UPDATE SET "
                "output = excluded.output, cached_at = excluded.cached_at, "
                "expires_at = excluded.expires_at",
                (
                    tool_ref,
                    tool_version,
                    input_hash,
                    output.model_dump_json(),
                    datetime.now(UTC).isoformat(),
                    time.time() + ttl_s,
                ),
            )
            conn.commit()
        except sqlite3.Error as exc:
            raise CacheBackendError(
                f"in_process tool-result cache store failed: {exc}",
                context={"backend": "in_process", "path": self._path},
                cause=exc,
            ) from exc


class RedisResultCache:
    """String keys + SETEX for TTL (docs/24 § Layer 3 backends). ``redis`` is
    imported lazily; missing package → structured CacheBackendError."""

    def __init__(
        self, url: str = "redis://localhost:6379/0", *, client: Any | None = None
    ) -> None:
        self._url = url
        self._client = client

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import redis.asyncio as redis_asyncio
        except ImportError as exc:
            raise CacheBackendError(
                "tool-result cache backend 'redis' requires the optional "
                "'redis' package (uv add redis) — not pinned by the framework",
                context={"backend": "redis", "missing_package": "redis"},
                cause=exc,
            ) from exc
        self._client = redis_asyncio.from_url(self._url)
        return self._client

    @staticmethod
    def _key(tool_ref: str, tool_version: str, input_hash: str) -> str:
        return f"foundry:toolcache:{tool_ref}@{tool_version}:{input_hash}"

    async def lookup(
        self, tool_ref: str, tool_version: str, input_hash: str
    ) -> CachedToolResult | None:
        client = self._get_client()
        try:
            raw = await client.get(self._key(tool_ref, tool_version, input_hash))
        except Exception as exc:
            raise CacheBackendError(
                f"redis tool-result cache lookup failed: {exc}",
                context={"backend": "redis", "url": self._url},
                cause=exc,
            ) from exc
        if raw is None:
            return None
        entry = json.loads(raw)
        return CachedToolResult(
            output=entry["output"],
            cached_at=datetime.fromisoformat(entry["cached_at"]),
        )

    async def store(
        self,
        tool_ref: str,
        tool_version: str,
        input_hash: str,
        output: BaseModel,
        ttl_s: int,
    ) -> None:
        client = self._get_client()
        payload = json.dumps(
            {
                "output": json.loads(output.model_dump_json()),
                "cached_at": datetime.now(UTC).isoformat(),
            }
        )
        try:
            await client.set(
                self._key(tool_ref, tool_version, input_hash), payload, ex=ttl_s
            )
        except Exception as exc:
            raise CacheBackendError(
                f"redis tool-result cache store failed: {exc}",
                context={"backend": "redis", "url": self._url},
                cause=exc,
            ) from exc


class PostgresResultCache:
    """Same Postgres instance as the pgvector semantic cache, different table,
    no vector index needed (docs/24 § Layer 3 backends)."""

    def __init__(
        self,
        *,
        pool: Any | None = None,
        dsn: str | None = None,
        table: str = "foundry_tool_result_cache",
    ) -> None:
        if pool is None and dsn is None:
            raise CacheBackendError(
                "postgres tool-result cache needs either a pool or a dsn",
                context={"backend": "postgres"},
            )
        self._pool = pool
        self._dsn = dsn
        self._table = table
        self._schema_ready = False

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        try:
            import asyncpg
        except ImportError as exc:
            raise CacheBackendError(
                "tool-result cache backend 'postgres' requires the optional "
                "'asyncpg' package (uv add asyncpg) — not pinned by the "
                "framework",
                context={"backend": "postgres", "missing_package": "asyncpg"},
                cause=exc,
            ) from exc
        try:
            self._pool = await asyncpg.create_pool(self._dsn)
        except Exception as exc:
            raise CacheBackendError(
                f"postgres tool-result cache could not connect: {exc}",
                context={"backend": "postgres"},
                cause=exc,
            ) from exc
        return self._pool

    async def _ensure_schema(self, pool: Any) -> None:
        if self._schema_ready:
            return
        await pool.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                tool_ref TEXT NOT NULL,
                tool_version TEXT NOT NULL,
                input_hash TEXT NOT NULL,
                output JSONB NOT NULL,
                cached_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL,
                PRIMARY KEY (tool_ref, tool_version, input_hash)
            )
            """
        )
        self._schema_ready = True

    async def lookup(
        self, tool_ref: str, tool_version: str, input_hash: str
    ) -> CachedToolResult | None:
        try:
            pool = await self._get_pool()
            await self._ensure_schema(pool)
            row = await pool.fetchrow(
                f"SELECT output, cached_at FROM {self._table} "
                f"WHERE tool_ref = $1 AND tool_version = $2 AND "
                f"input_hash = $3 AND expires_at > now()",
                tool_ref,
                tool_version,
                input_hash,
            )
        except CacheBackendError:
            raise
        except Exception as exc:
            raise CacheBackendError(
                f"postgres tool-result cache lookup failed: {exc}",
                context={"backend": "postgres", "table": self._table},
                cause=exc,
            ) from exc
        if row is None:
            return None
        raw = row["output"]
        output = json.loads(raw) if isinstance(raw, str) else raw
        cached_at = row["cached_at"]
        return CachedToolResult(
            output=output,
            cached_at=(
                cached_at
                if isinstance(cached_at, datetime)
                else datetime.fromisoformat(str(cached_at))
            ),
        )

    async def store(
        self,
        tool_ref: str,
        tool_version: str,
        input_hash: str,
        output: BaseModel,
        ttl_s: int,
    ) -> None:
        try:
            pool = await self._get_pool()
            await self._ensure_schema(pool)
            await pool.execute(
                f"INSERT INTO {self._table} (tool_ref, tool_version, "
                f"input_hash, output, expires_at) "
                f"VALUES ($1, $2, $3, $4::jsonb, "
                f"now() + make_interval(secs => $5)) "
                f"ON CONFLICT (tool_ref, tool_version, input_hash) "
                f"DO UPDATE SET output = EXCLUDED.output, "
                f"cached_at = now(), expires_at = EXCLUDED.expires_at",
                tool_ref,
                tool_version,
                input_hash,
                output.model_dump_json(),
                float(ttl_s),
            )
        except CacheBackendError:
            raise
        except Exception as exc:
            raise CacheBackendError(
                f"postgres tool-result cache store failed: {exc}",
                context={"backend": "postgres", "table": self._table},
                cause=exc,
            ) from exc


__all__ = ["InProcessResultCache", "PostgresResultCache", "RedisResultCache"]
