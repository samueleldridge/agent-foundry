"""Semantic cache backends + key construction (docs/24 § Test expectations)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from foundry.cache import (
    InProcessSemanticCache,
    PgVectorSemanticCache,
    RedisSemanticCache,
    build_semantic_cache_key,
    cosine_similarity,
    messages_structural_hash,
    model_binding_hash,
    tools_hash,
)
from foundry.core import (
    Embedding,
    FoundryMessage,
    MessageRole,
    ModelResponse,
    SemanticCacheKey,
    StopReason,
    TextBlock,
    TokenUsage,
    ToolUseBlock,
)
from foundry.core.errors import CacheBackendError
from foundry.providers import ModelBinding, ToolSchema

DIMS = 4


def _embedding(vector: list[float]) -> Embedding:
    return Embedding(
        vector=vector, dimensions=len(vector), model="fake-embed",
        input_tokens=3, latency_ms=1,
    )


def _key(
    vector: list[float],
    *,
    agent: str = "rag_agent",
    version: str = "hash-v1",
    tools: str = "tools-a",
) -> SemanticCacheKey:
    return SemanticCacheKey(
        agent_name=agent,
        agent_version=version,
        model_binding_hash="mb-1",
        tools_hash=tools,
        messages_structural_hash="struct-1",
        messages_embedding=_embedding(vector),
    )


def _response(cost: str = "0.001") -> ModelResponse:
    from decimal import Decimal

    return ModelResponse(
        message=FoundryMessage(
            role=MessageRole.ASSISTANT, content=[TextBlock(text="cached answer")]
        ),
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(input_tokens=100, output_tokens=40),
        model="fake-model",
        provider="fake",
        latency_ms=5,
        cost_estimate_usd=Decimal(cost),
    )


# --- key construction (docs/24 unit expectations 1-2) ---------------------------


class _FixedEmbedder:
    name = "fake:embed"
    model = "embed"
    capabilities = None  # unused

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], str]] = []

    async def embed(self, inputs: list[str], purpose: str = "document") -> list[Embedding]:
        self.calls.append((inputs, purpose))
        return [_embedding([1.0, 0.0, 0.0, 0.0]) for _ in inputs]


def _messages(text: str) -> list[FoundryMessage]:
    return [
        FoundryMessage(role=MessageRole.SYSTEM, content=[TextBlock(text="sys")]),
        FoundryMessage(role=MessageRole.USER, content=[TextBlock(text=text)]),
    ]


@pytest.mark.unit
async def test_key_construction_is_deterministic_and_embeds_as_query() -> None:
    binding = ModelBinding(provider="anthropic", model="claude-haiku-4-5")
    tools = [ToolSchema(name="b", description="", input_schema={}),
             ToolSchema(name="a", description="", input_schema={})]
    embedder = _FixedEmbedder()
    key1 = await build_semantic_cache_key(
        agent_name="a", agent_version="v", model_binding=binding,
        tools=tools, messages=_messages("hi"), embedder=embedder,  # type: ignore[arg-type]
    )
    key2 = await build_semantic_cache_key(
        agent_name="a", agent_version="v", model_binding=binding,
        tools=list(reversed(tools)), messages=_messages("hi"),
        embedder=embedder,  # type: ignore[arg-type]
    )
    # structural fields identical (tools hash is order-independent)
    assert key1.bucket() == key2.bucket()
    # both stored and lookup keys embed with purpose='query' (docs/24)
    assert all(purpose == "query" for _, purpose in embedder.calls)


@pytest.mark.unit
def test_different_tools_hash_means_different_bucket() -> None:
    tools_a = [ToolSchema(name="a", description="", input_schema={})]
    tools_b = [*tools_a, ToolSchema(name="b", description="", input_schema={})]
    assert tools_hash(tools_a) != tools_hash(tools_b)
    key_a = _key([1.0, 0, 0, 0], tools=tools_hash(tools_a))
    key_b = _key([1.0, 0, 0, 0], tools=tools_hash(tools_b))
    assert key_a.bucket() != key_b.bucket()


@pytest.mark.unit
def test_structural_hash_catches_new_block_types_not_text_changes() -> None:
    text_only = _messages("hello")
    other_text = _messages("goodbye")
    with_tool_use = _messages("hello")
    with_tool_use[1] = FoundryMessage(
        role=MessageRole.USER,
        content=[TextBlock(text="hello"),
                 ToolUseBlock(id="t1", name="x", input={})],
    )
    assert messages_structural_hash(text_only) == messages_structural_hash(other_text)
    assert messages_structural_hash(text_only) != messages_structural_hash(with_tool_use)


@pytest.mark.unit
def test_model_binding_hash_separates_temperature() -> None:
    cold = ModelBinding(
        provider="anthropic", model="m", settings={"temperature": 0.0}  # type: ignore[arg-type]
    )
    warm = ModelBinding(
        provider="anthropic", model="m", settings={"temperature": 0.7}  # type: ignore[arg-type]
    )
    assert model_binding_hash(cold) != model_binding_hash(warm)


@pytest.mark.unit
def test_cosine_similarity_basics() -> None:
    assert cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cosine_similarity([1.0], [1.0, 0.0]) == -1.0  # dim mismatch


# --- in_process backend ---------------------------------------------------------


@pytest.mark.unit
async def test_threshold_enforcement_094_misses_096_hits() -> None:
    cache = InProcessSemanticCache(":memory:")
    stored_vector = [1.0, 0.0, 0.0, 0.0]
    await cache.store(_key(stored_vector), _response(), ttl_s=60)

    # craft a probe whose cosine with stored is ~0.94
    probe_094 = [0.94, (1 - 0.94**2) ** 0.5, 0.0, 0.0]
    hit = await cache.lookup(_key(probe_094), threshold=0.95)
    assert hit is None
    assert cache.last_top_similarity == pytest.approx(0.94, abs=1e-6)

    probe_096 = [0.96, (1 - 0.96**2) ** 0.5, 0.0, 0.0]
    hit = await cache.lookup(_key(probe_096), threshold=0.95)
    assert hit is not None
    assert hit.similarity == pytest.approx(0.96, abs=1e-6)
    assert hit.response.usage.input_tokens == 100


@pytest.mark.unit
async def test_bucket_separation_same_vector_different_tools_misses() -> None:
    cache = InProcessSemanticCache(":memory:")
    vector = [1.0, 0.0, 0.0, 0.0]
    await cache.store(_key(vector, tools="tools-a"), _response(), ttl_s=60)
    assert await cache.lookup(_key(vector, tools="tools-b"), 0.5) is None
    assert await cache.lookup(_key(vector, tools="tools-a"), 0.5) is not None


@pytest.mark.unit
async def test_agent_version_change_is_a_different_bucket_and_invalidate_evicts() -> None:
    cache = InProcessSemanticCache(":memory:")
    vector = [1.0, 0.0, 0.0, 0.0]
    await cache.store(_key(vector, version="hash-v1"), _response(), ttl_s=60)
    # version bump alone already misses (agent_version is in the bucket)
    assert await cache.lookup(_key(vector, version="hash-v2"), 0.5) is None
    # explicit invalidation evicts the old entries too
    await cache.invalidate("rag_agent")
    assert await cache.lookup(_key(vector, version="hash-v1"), 0.5) is None


@pytest.mark.unit
async def test_version_markers_roundtrip() -> None:
    cache = InProcessSemanticCache(":memory:")
    assert await cache.version_marker("rag_agent") is None
    await cache.set_version_marker("rag_agent", "hash-v1")
    assert await cache.version_marker("rag_agent") == "hash-v1"
    await cache.set_version_marker("rag_agent", "hash-v2")
    assert await cache.version_marker("rag_agent") == "hash-v2"


@pytest.mark.unit
async def test_ttl_expiry_and_lru_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = InProcessSemanticCache(":memory:", max_entries=2)
    vector = [1.0, 0.0, 0.0, 0.0]
    now = 1000.0
    monkeypatch.setattr("foundry.cache.semantic.time.time", lambda: now)
    await cache.store(_key(vector), _response(), ttl_s=10)
    now = 1011.0  # past the TTL
    assert await cache.lookup(_key(vector), 0.5) is None

    # LRU cap: 3 stores with max_entries=2 → oldest evicted
    v1, v2, v3 = [1.0, 0, 0, 0], [0, 1.0, 0, 0], [0, 0, 1.0, 0]
    now = 2000.0
    await cache.store(_key(v1), _response(), ttl_s=100)
    now = 2001.0
    await cache.store(_key(v2), _response(), ttl_s=100)
    now = 2002.0
    await cache.store(_key(v3), _response(), ttl_s=100)
    assert await cache.lookup(_key(v1), 0.99) is None  # evicted
    assert await cache.lookup(_key(v3), 0.99) is not None


@pytest.mark.unit
async def test_corrupted_entry_is_evicted_and_treated_as_miss(tmp_path: Path) -> None:
    db = tmp_path / "sem.db"
    cache = InProcessSemanticCache(db)
    vector = [1.0, 0.0, 0.0, 0.0]
    await cache.store(_key(vector), _response(), ttl_s=60)
    conn = cache._connect()
    conn.execute("UPDATE semantic_entries SET response = 'not json {'")
    conn.commit()
    assert await cache.lookup(_key(vector), 0.5) is None  # evict + miss
    rows = conn.execute("SELECT COUNT(*) FROM semantic_entries").fetchone()
    assert rows[0] == 0


@pytest.mark.unit
async def test_unusable_sqlite_path_raises_cache_backend_error(tmp_path: Path) -> None:
    cache = InProcessSemanticCache(tmp_path)  # a DIRECTORY, not a db file
    with pytest.raises(CacheBackendError):
        await cache.lookup(_key([1.0, 0, 0, 0]), 0.9)


# --- redis backend shape (fake client; the real package is not installed) --------


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.sets: dict[str, set[str]] = {}

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.kv[key] = value

    async def sadd(self, key: str, *members: str) -> None:
        self.sets.setdefault(key, set()).update(members)

    async def smembers(self, key: str) -> set[str]:
        return set(self.sets.get(key, set()))

    async def srem(self, key: str, *members: str) -> None:
        self.sets.get(key, set()).difference_update(members)

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.kv.pop(key, None)
            self.sets.pop(key, None)


@pytest.mark.unit
async def test_redis_backend_store_lookup_invalidate_and_markers() -> None:
    fake = _FakeRedis()
    cache = RedisSemanticCache(client=fake, scope_key="agent:p/a")
    vector = [1.0, 0.0, 0.0, 0.0]
    await cache.store(_key(vector), _response(), ttl_s=60)
    hit = await cache.lookup(_key(vector), 0.95)
    assert hit is not None and hit.similarity == pytest.approx(1.0)
    await cache.set_version_marker("rag_agent", "hash-v1")
    assert await cache.version_marker("rag_agent") == "hash-v1"
    await cache.invalidate("rag_agent")
    assert await cache.lookup(_key(vector), 0.5) is None


@pytest.mark.unit
async def test_redis_backend_without_package_raises_structured_error() -> None:
    cache = RedisSemanticCache()  # no injected client; package not installed
    with pytest.raises(CacheBackendError) as excinfo:
        await cache.lookup(_key([1.0, 0, 0, 0]), 0.9)
    assert excinfo.value.context["missing_package"] == "redis"


# --- pgvector backend shape (fake pool; asyncpg is not installed) ------------------


class _FakePgPool:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.row: dict[str, Any] | None = None
        self.marker: str | None = None

    async def execute(self, sql: str, *args: Any) -> None:
        self.executed.append((sql, args))

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.executed.append((sql, args))
        return self.row

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.executed.append((sql, args))
        return self.marker


@pytest.mark.unit
async def test_pgvector_backend_sql_shapes_and_hit_path() -> None:
    pool = _FakePgPool()
    cache = PgVectorSemanticCache(pool=pool, dimensions=DIMS, scope_key="global")
    vector = [1.0, 0.0, 0.0, 0.0]
    await cache.store(_key(vector), _response(), ttl_s=60)
    insert_sql = pool.executed[-1][0]
    assert "INSERT INTO foundry_semantic_cache" in insert_sql
    assert "vector" in insert_sql

    pool.row = {
        "response": json.loads(_response().model_dump_json()),
        "input_preview": None,
        "cached_at": datetime.now(UTC),
        "similarity": 0.97,
    }
    hit = await cache.lookup(_key(vector), 0.95)
    assert hit is not None and hit.similarity == pytest.approx(0.97)
    lookup_sql = pool.executed[-1][0]
    assert "embedding <=>" in lookup_sql and "expires_at > now()" in lookup_sql

    pool.row = None
    assert await cache.lookup(_key(vector), 0.95) is None


@pytest.mark.unit
async def test_pgvector_backend_without_package_raises_structured_error() -> None:
    cache = PgVectorSemanticCache(dsn="postgresql://example/db", dimensions=DIMS)
    with pytest.raises(CacheBackendError) as excinfo:
        await cache.lookup(_key([1.0, 0, 0, 0]), 0.9)
    assert excinfo.value.context["missing_package"] == "asyncpg"
