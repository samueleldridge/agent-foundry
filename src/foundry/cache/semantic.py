"""SemanticCache backends: in_process (SQLite + local vector math), redis,
pgvector (docs/24 § Backends).

All three implement the core ``SemanticCache`` protocol plus the version-
marker extension (``version_marker`` / ``set_version_marker``) the runtime
uses for compile-time agent-version invalidation (correctness rule 1).

Optional heavy dependencies stay optional: ``redis`` and ``asyncpg`` are
imported lazily and raise a structured ``CacheBackendError`` naming the
missing package (the established catalog/connections pattern). Every backend
error surfaces as ``CacheBackendError`` — the integration layer fails open.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import ValidationError

from foundry.cache.keys import cosine_similarity
from foundry.core import (
    ModelResponse,
    SemanticCacheHit,
    SemanticCacheKey,
)
from foundry.core.errors import CacheBackendError


@runtime_checkable
class VersionMarkedSemanticCache(Protocol):
    """SemanticCache + the agent-version marker the runtime compares at
    startup to decide whether to invalidate (docs/24 correctness rule 1)."""

    async def lookup(
        self, key: SemanticCacheKey, threshold: float
    ) -> SemanticCacheHit | None: ...

    async def store(
        self, key: SemanticCacheKey, response: ModelResponse, ttl_s: int
    ) -> None: ...

    async def invalidate(self, agent_name: str) -> None: ...

    async def version_marker(self, agent_name: str) -> str | None: ...

    async def set_version_marker(self, agent_name: str, version: str) -> None: ...


# --- in_process (SQLite + plain-Python cosine) ---------------------------------------


class InProcessSemanticCache:
    """Dev/test/single-worker backend (docs/24: per-worker, volatile-ish).

    SQLite file (or ':memory:') + brute-force cosine inside the exact-match
    bucket. FAISS is deliberately NOT required — corpus sizes at dev scale
    don't need an ANN index, and optional heavy deps stay optional.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        scope_key: str = "global",
        max_entries: int = 10_000,
    ) -> None:
        self._path = str(path)
        self._scope_key = scope_key
        self._max_entries = max_entries
        self._conn: sqlite3.Connection | None = None

    # -- plumbing ------------------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        if self._conn is not None:
            return self._conn
        try:
            if self._path != ":memory:":
                Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._path)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS semantic_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope_key TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    bucket TEXT NOT NULL,
                    embedding TEXT NOT NULL,
                    response TEXT NOT NULL,
                    input_preview TEXT,
                    cached_at TEXT NOT NULL,
                    expires_at REAL NOT NULL,
                    last_access REAL NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS semantic_markers (
                    scope_key TEXT NOT NULL,
                    agent_name TEXT NOT NULL,
                    agent_version TEXT NOT NULL,
                    PRIMARY KEY (scope_key, agent_name)
                )
                """
            )
            conn.commit()
        except (sqlite3.Error, OSError) as exc:
            raise CacheBackendError(
                f"in_process semantic cache unavailable at {self._path!r}: {exc}",
                context={"backend": "in_process", "path": self._path},
                cause=exc,
            ) from exc
        self._conn = conn
        return conn

    # -- protocol ------------------------------------------------------------

    async def lookup(
        self, key: SemanticCacheKey, threshold: float
    ) -> SemanticCacheHit | None:
        conn = self._connect()
        now = time.time()
        try:
            conn.execute(
                "DELETE FROM semantic_entries WHERE expires_at <= ?", (now,)
            )
            rows = conn.execute(
                "SELECT id, embedding, response, input_preview, cached_at "
                "FROM semantic_entries WHERE scope_key = ? AND bucket = ?",
                (self._scope_key, key.bucket()),
            ).fetchall()
            conn.commit()
        except sqlite3.Error as exc:
            raise CacheBackendError(
                f"in_process semantic cache lookup failed: {exc}",
                context={"backend": "in_process", "path": self._path},
                cause=exc,
            ) from exc

        best: tuple[float, Any] | None = None
        corrupted: list[int] = []
        for row in rows:
            row_id, embedding_json, response_json, preview, cached_at = row
            try:
                vector = json.loads(embedding_json)
                similarity = cosine_similarity(
                    key.messages_embedding.vector, vector
                )
            except (json.JSONDecodeError, TypeError):
                corrupted.append(row_id)
                continue
            if best is None or similarity > best[0]:
                best = (similarity, (row_id, response_json, preview, cached_at))
        if corrupted:  # docs/24: corrupted entry → eviction + miss
            conn.executemany(
                "DELETE FROM semantic_entries WHERE id = ?",
                [(row_id,) for row_id in corrupted],
            )
            conn.commit()

        self.last_top_similarity = best[0] if best is not None else 0.0
        if best is None or best[0] < threshold:
            return None
        row_id, response_json, preview, cached_at = best[1]
        try:
            response = ModelResponse.model_validate_json(response_json)
        except ValidationError:
            # CacheCorruptedEntry semantics: evict + miss.
            conn.execute("DELETE FROM semantic_entries WHERE id = ?", (row_id,))
            conn.commit()
            self.last_top_similarity = 0.0
            return None
        conn.execute(
            "UPDATE semantic_entries SET last_access = ? WHERE id = ?",
            (now, row_id),
        )
        conn.commit()
        return SemanticCacheHit(
            response=response,
            similarity=best[0],
            cached_at=datetime.fromisoformat(cached_at),
            original_input_preview=preview,
        )

    async def store(
        self, key: SemanticCacheKey, response: ModelResponse, ttl_s: int
    ) -> None:
        conn = self._connect()
        now = time.time()
        try:
            conn.execute(
                "INSERT INTO semantic_entries (scope_key, agent_name, bucket, "
                "embedding, response, input_preview, cached_at, expires_at, "
                "last_access) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    self._scope_key,
                    key.agent_name,
                    key.bucket(),
                    json.dumps(key.messages_embedding.vector),
                    response.model_dump_json(),
                    None,
                    datetime.now(UTC).isoformat(),
                    now + ttl_s,
                    now,
                ),
            )
            # LRU cap (docs/12 SemanticCacheConfig.max_entries).
            conn.execute(
                "DELETE FROM semantic_entries WHERE scope_key = ? AND id NOT IN "
                "(SELECT id FROM semantic_entries WHERE scope_key = ? "
                "ORDER BY last_access DESC LIMIT ?)",
                (self._scope_key, self._scope_key, self._max_entries),
            )
            conn.commit()
        except sqlite3.Error as exc:
            raise CacheBackendError(
                f"in_process semantic cache store failed: {exc}",
                context={"backend": "in_process", "path": self._path},
                cause=exc,
            ) from exc

    async def invalidate(self, agent_name: str) -> None:
        conn = self._connect()
        try:
            if agent_name == "*":
                conn.execute(
                    "DELETE FROM semantic_entries WHERE scope_key = ?",
                    (self._scope_key,),
                )
            else:
                conn.execute(
                    "DELETE FROM semantic_entries WHERE scope_key = ? AND "
                    "agent_name = ?",
                    (self._scope_key, agent_name),
                )
            conn.commit()
        except sqlite3.Error as exc:
            raise CacheBackendError(
                f"in_process semantic cache invalidate failed: {exc}",
                context={"backend": "in_process", "path": self._path},
                cause=exc,
            ) from exc

    # -- version markers -------------------------------------------------------

    async def version_marker(self, agent_name: str) -> str | None:
        conn = self._connect()
        row = conn.execute(
            "SELECT agent_version FROM semantic_markers WHERE scope_key = ? "
            "AND agent_name = ?",
            (self._scope_key, agent_name),
        ).fetchone()
        return row[0] if row else None

    async def set_version_marker(self, agent_name: str, version: str) -> None:
        conn = self._connect()
        conn.execute(
            "INSERT INTO semantic_markers (scope_key, agent_name, agent_version) "
            "VALUES (?, ?, ?) ON CONFLICT (scope_key, agent_name) "
            "DO UPDATE SET agent_version = excluded.agent_version",
            (self._scope_key, agent_name, version),
        )
        conn.commit()

    last_top_similarity: float = 0.0
    """Best below-threshold candidate from the most recent lookup — feeds the
    cache.semantic.miss event's top_similarity dimension."""


# --- redis (Redis Stack) ---------------------------------------------------------


class RedisSemanticCache:
    """Multi-worker shared backend over Redis (docs/24 § Backends).

    Entries are JSON hashes indexed per exact-match bucket (a SET of entry
    keys); similarity is computed client-side over the bucket's members. A
    production deployment at scale would move similarity into RediSearch's
    vector index — the protocol surface stays identical, so that upgrade is
    internal to this class. ``redis`` is imported lazily; a missing package
    surfaces as a structured ``CacheBackendError`` (fail-open at run time).
    """

    def __init__(
        self,
        url: str = "redis://localhost:6379/0",
        *,
        scope_key: str = "global",
        max_entries: int = 10_000,
        client: Any | None = None,
    ) -> None:
        self._url = url
        self._scope_key = scope_key
        self._max_entries = max_entries
        self._client = client
        self.last_top_similarity = 0.0

    def _prefix(self) -> str:
        return f"foundry:semcache:{self._scope_key}"

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import redis.asyncio as redis_asyncio
        except ImportError as exc:
            raise CacheBackendError(
                "semantic_cache backend 'redis' requires the optional 'redis' "
                "package (uv add redis) — not pinned by the framework",
                context={"backend": "redis", "missing_package": "redis"},
                cause=exc,
            ) from exc
        self._client = redis_asyncio.from_url(self._url)
        return self._client

    async def lookup(
        self, key: SemanticCacheKey, threshold: float
    ) -> SemanticCacheHit | None:
        client = self._get_client()
        bucket_key = f"{self._prefix()}:bucket:{key.bucket()}"
        try:
            members = await client.smembers(bucket_key)
            best: tuple[float, dict[str, Any]] | None = None
            stale: list[str] = []
            for member in members:
                name = member.decode() if isinstance(member, bytes) else member
                raw = await client.get(name)
                if raw is None:  # TTL-expired entry; index is stale
                    stale.append(name)
                    continue
                entry = json.loads(raw)
                similarity = cosine_similarity(
                    key.messages_embedding.vector, entry["embedding"]
                )
                if best is None or similarity > best[0]:
                    best = (similarity, entry)
            if stale:
                await client.srem(bucket_key, *stale)
        except CacheBackendError:
            raise
        except Exception as exc:
            raise CacheBackendError(
                f"redis semantic cache lookup failed: {exc}",
                context={"backend": "redis", "url": self._url},
                cause=exc,
            ) from exc
        self.last_top_similarity = best[0] if best is not None else 0.0
        if best is None or best[0] < threshold:
            return None
        try:
            response = ModelResponse.model_validate(best[1]["response"])
        except ValidationError:
            self.last_top_similarity = 0.0
            return None
        return SemanticCacheHit(
            response=response,
            similarity=best[0],
            cached_at=datetime.fromisoformat(best[1]["cached_at"]),
            original_input_preview=best[1].get("input_preview"),
        )

    async def store(
        self, key: SemanticCacheKey, response: ModelResponse, ttl_s: int
    ) -> None:
        client = self._get_client()
        entry_key = (
            f"{self._prefix()}:entry:{key.bucket()}:{time.time_ns()}"
        )
        bucket_key = f"{self._prefix()}:bucket:{key.bucket()}"
        agent_key = f"{self._prefix()}:agent:{key.agent_name}"
        payload = json.dumps(
            {
                "embedding": key.messages_embedding.vector,
                "response": json.loads(response.model_dump_json()),
                "cached_at": datetime.now(UTC).isoformat(),
                "input_preview": None,
            }
        )
        try:
            await client.set(entry_key, payload, ex=ttl_s)
            await client.sadd(bucket_key, entry_key)
            await client.sadd(agent_key, entry_key, bucket_key)
        except Exception as exc:
            raise CacheBackendError(
                f"redis semantic cache store failed: {exc}",
                context={"backend": "redis", "url": self._url},
                cause=exc,
            ) from exc

    async def invalidate(self, agent_name: str) -> None:
        client = self._get_client()
        agent_key = f"{self._prefix()}:agent:{agent_name}"
        try:
            members = await client.smembers(agent_key)
            names = [
                m.decode() if isinstance(m, bytes) else m for m in members
            ]
            if names:
                await client.delete(*names)
            await client.delete(agent_key)
        except Exception as exc:
            raise CacheBackendError(
                f"redis semantic cache invalidate failed: {exc}",
                context={"backend": "redis", "url": self._url},
                cause=exc,
            ) from exc

    async def version_marker(self, agent_name: str) -> str | None:
        client = self._get_client()
        try:
            raw = await client.get(f"{self._prefix()}:marker:{agent_name}")
        except Exception as exc:
            raise CacheBackendError(
                f"redis semantic cache marker read failed: {exc}",
                context={"backend": "redis", "url": self._url},
                cause=exc,
            ) from exc
        if raw is None:
            return None
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    async def set_version_marker(self, agent_name: str, version: str) -> None:
        client = self._get_client()
        try:
            await client.set(f"{self._prefix()}:marker:{agent_name}", version)
        except Exception as exc:
            raise CacheBackendError(
                f"redis semantic cache marker write failed: {exc}",
                context={"backend": "redis", "url": self._url},
                cause=exc,
            ) from exc


# --- pgvector -----------------------------------------------------------------------


class PgVectorSemanticCache:
    """Shared backend on Postgres + pgvector (docs/24 § Backends) — one fewer
    service when the deployment already runs Postgres.

    Accepts either an existing asyncpg-compatible pool (e.g. the client built
    by the Phase 2a ``catalog/pgvector`` connection) or a DSN; the DSN path
    lazily imports ``asyncpg`` and raises a structured error when missing.
    ``dimensions`` fixes the vector column width — the compile-time dimension
    check runs against it before any call (docs/24 § Dimension compatibility).
    """

    def __init__(
        self,
        *,
        pool: Any | None = None,
        dsn: str | None = None,
        table: str = "foundry_semantic_cache",
        scope_key: str = "global",
        dimensions: int = 1024,
        max_entries: int = 10_000,
    ) -> None:
        if pool is None and dsn is None:
            raise CacheBackendError(
                "pgvector semantic cache needs either a pool or a dsn",
                context={"backend": "pgvector"},
            )
        self._pool = pool
        self._dsn = dsn
        self._table = table
        self._scope_key = scope_key
        self.dimensions = dimensions
        self._max_entries = max_entries
        self._schema_ready = False
        self.last_top_similarity = 0.0

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool
        try:
            import asyncpg
        except ImportError as exc:
            raise CacheBackendError(
                "semantic_cache backend 'pgvector' requires the optional "
                "'asyncpg' package (uv add asyncpg) — not pinned by the "
                "framework",
                context={"backend": "pgvector", "missing_package": "asyncpg"},
                cause=exc,
            ) from exc
        try:
            self._pool = await asyncpg.create_pool(self._dsn)
        except Exception as exc:
            raise CacheBackendError(
                f"pgvector semantic cache could not connect: {exc}",
                context={"backend": "pgvector"},
                cause=exc,
            ) from exc
        return self._pool

    async def _ensure_schema(self, pool: Any) -> None:
        if self._schema_ready:
            return
        await pool.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {self._table} (
                id BIGSERIAL PRIMARY KEY,
                scope_key TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                bucket TEXT NOT NULL,
                embedding vector({self.dimensions}) NOT NULL,
                response JSONB NOT NULL,
                input_preview TEXT,
                cached_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                expires_at TIMESTAMPTZ NOT NULL
            )
            """
        )
        self._schema_ready = True

    @staticmethod
    def _vector_literal(vector: list[float]) -> str:
        return "[" + ",".join(repr(v) for v in vector) + "]"

    async def lookup(
        self, key: SemanticCacheKey, threshold: float
    ) -> SemanticCacheHit | None:
        try:
            pool = await self._get_pool()
            await self._ensure_schema(pool)
            row = await pool.fetchrow(
                f"SELECT response, input_preview, cached_at, "
                f"1 - (embedding <=> $3::vector) AS similarity "
                f"FROM {self._table} "
                f"WHERE scope_key = $1 AND bucket = $2 AND expires_at > now() "
                f"ORDER BY embedding <=> $3::vector LIMIT 1",
                self._scope_key,
                key.bucket(),
                self._vector_literal(key.messages_embedding.vector),
            )
        except CacheBackendError:
            raise
        except Exception as exc:
            raise CacheBackendError(
                f"pgvector semantic cache lookup failed: {exc}",
                context={"backend": "pgvector", "table": self._table},
                cause=exc,
            ) from exc
        if row is None:
            self.last_top_similarity = 0.0
            return None
        similarity = float(row["similarity"])
        self.last_top_similarity = similarity
        if similarity < threshold:
            return None
        raw = row["response"]
        try:
            response = ModelResponse.model_validate(
                json.loads(raw) if isinstance(raw, str) else raw
            )
        except (ValidationError, json.JSONDecodeError):
            self.last_top_similarity = 0.0
            return None
        cached_at = row["cached_at"]
        return SemanticCacheHit(
            response=response,
            similarity=similarity,
            cached_at=(
                cached_at
                if isinstance(cached_at, datetime)
                else datetime.fromisoformat(str(cached_at))
            ),
            original_input_preview=row["input_preview"],
        )

    async def store(
        self, key: SemanticCacheKey, response: ModelResponse, ttl_s: int
    ) -> None:
        try:
            pool = await self._get_pool()
            await self._ensure_schema(pool)
            await pool.execute(
                f"INSERT INTO {self._table} (scope_key, agent_name, bucket, "
                f"embedding, response, expires_at) "
                f"VALUES ($1, $2, $3, $4::vector, $5::jsonb, "
                f"now() + make_interval(secs => $6))",
                self._scope_key,
                key.agent_name,
                key.bucket(),
                self._vector_literal(key.messages_embedding.vector),
                response.model_dump_json(),
                float(ttl_s),
            )
        except CacheBackendError:
            raise
        except Exception as exc:
            raise CacheBackendError(
                f"pgvector semantic cache store failed: {exc}",
                context={"backend": "pgvector", "table": self._table},
                cause=exc,
            ) from exc

    async def invalidate(self, agent_name: str) -> None:
        try:
            pool = await self._get_pool()
            await self._ensure_schema(pool)
            if agent_name == "*":
                await pool.execute(
                    f"DELETE FROM {self._table} WHERE scope_key = $1",
                    self._scope_key,
                )
            else:
                await pool.execute(
                    f"DELETE FROM {self._table} WHERE scope_key = $1 AND "
                    f"agent_name = $2",
                    self._scope_key,
                    agent_name,
                )
        except CacheBackendError:
            raise
        except Exception as exc:
            raise CacheBackendError(
                f"pgvector semantic cache invalidate failed: {exc}",
                context={"backend": "pgvector", "table": self._table},
                cause=exc,
            ) from exc

    async def version_marker(self, agent_name: str) -> str | None:
        try:
            pool = await self._get_pool()
            await pool.execute(
                f"CREATE TABLE IF NOT EXISTS {self._table}_markers ("
                "scope_key TEXT NOT NULL, agent_name TEXT NOT NULL, "
                "agent_version TEXT NOT NULL, "
                "PRIMARY KEY (scope_key, agent_name))"
            )
            value = await pool.fetchval(
                f"SELECT agent_version FROM {self._table}_markers "
                f"WHERE scope_key = $1 AND agent_name = $2",
                self._scope_key,
                agent_name,
            )
        except CacheBackendError:
            raise
        except Exception as exc:
            raise CacheBackendError(
                f"pgvector semantic cache marker read failed: {exc}",
                context={"backend": "pgvector", "table": self._table},
                cause=exc,
            ) from exc
        return str(value) if value is not None else None

    async def set_version_marker(self, agent_name: str, version: str) -> None:
        try:
            pool = await self._get_pool()
            await pool.execute(
                f"INSERT INTO {self._table}_markers (scope_key, agent_name, "
                f"agent_version) VALUES ($1, $2, $3) "
                f"ON CONFLICT (scope_key, agent_name) "
                f"DO UPDATE SET agent_version = EXCLUDED.agent_version",
                self._scope_key,
                agent_name,
                version,
            )
        except CacheBackendError:
            raise
        except Exception as exc:
            raise CacheBackendError(
                f"pgvector semantic cache marker write failed: {exc}",
                context={"backend": "pgvector", "table": self._table},
                cause=exc,
            ) from exc


__all__ = [
    "InProcessSemanticCache",
    "PgVectorSemanticCache",
    "RedisSemanticCache",
    "VersionMarkedSemanticCache",
]
