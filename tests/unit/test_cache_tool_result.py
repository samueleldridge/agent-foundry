"""ResultCache backends + the dispatcher's cache steps (docs/24 § Layer 3)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from foundry.cache import InProcessResultCache, PostgresResultCache, RedisResultCache
from foundry.core import (
    CacheBundle,
    CachedToolResult,
    RegisteredTool,
    RetryPolicy,
    Session,
    ToolDescriptor,
    ToolRegistry,
    scoped_input_hash,
)
from foundry.core.errors import CacheBackendError
from foundry.core.events import (
    ToolCacheHit,
    ToolCacheMiss,
    ToolCacheStore,
    ToolCompleted,
    ToolStarted,
    WarningEvent,
)
from foundry.core.tool import RunContext


class EchoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class EchoOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    echoed: str


# --- scope keys -------------------------------------------------------------------


@pytest.mark.unit
def test_scoped_input_hash_isolates_agent_project_global() -> None:
    agent_a = scoped_input_hash("agent", "proj", "agent_a", "h1")
    agent_b = scoped_input_hash("agent", "proj", "agent_b", "h1")
    project = scoped_input_hash("project", "proj", "agent_a", "h1")
    other_project = scoped_input_hash("project", "other", "agent_a", "h1")
    global_a = scoped_input_hash("global", "proj", "agent_a", "h1")
    global_b = scoped_input_hash("global", "other", "agent_b", "h1")
    assert agent_a != agent_b            # agent scope: no cross-agent sharing
    assert project != other_project      # project scope: no cross-project
    assert global_a == global_b          # global: shared everywhere


# --- in_process backend --------------------------------------------------------------


@pytest.mark.unit
async def test_in_process_roundtrip_and_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    cache = InProcessResultCache(":memory:")
    now = 1000.0
    monkeypatch.setattr("foundry.cache.tool_result.time.time", lambda: now)
    await cache.store("local/echo", "v1", "key-1", EchoOut(echoed="hi"), ttl_s=60)
    hit = await cache.lookup("local/echo", "v1", "key-1")
    assert isinstance(hit, CachedToolResult)
    assert hit.output == {"echoed": "hi"}
    assert await cache.lookup("local/echo", "v2", "key-1") is None  # version-keyed
    now = 1061.0
    assert await cache.lookup("local/echo", "v1", "key-1") is None  # expired


@pytest.mark.unit
async def test_in_process_store_overwrites_existing_entry() -> None:
    cache = InProcessResultCache(":memory:")
    await cache.store("local/echo", "v1", "k", EchoOut(echoed="old"), ttl_s=60)
    await cache.store("local/echo", "v1", "k", EchoOut(echoed="new"), ttl_s=60)
    hit = await cache.lookup("local/echo", "v1", "k")
    assert hit is not None and hit.output == {"echoed": "new"}


# --- redis / postgres shapes (packages not installed; fakes) -------------------------


class _FakeRedis:
    def __init__(self) -> None:
        self.kv: dict[str, str] = {}
        self.last_ex: int | None = None

    async def get(self, key: str) -> str | None:
        return self.kv.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.kv[key] = value
        self.last_ex = ex


@pytest.mark.unit
async def test_redis_result_cache_uses_setex_semantics() -> None:
    fake = _FakeRedis()
    cache = RedisResultCache(client=fake)
    await cache.store("catalog/t", "v1", "k", EchoOut(echoed="x"), ttl_s=300)
    assert fake.last_ex == 300
    hit = await cache.lookup("catalog/t", "v1", "k")
    assert hit is not None and hit.output == {"echoed": "x"}
    assert await cache.lookup("catalog/t", "v1", "other") is None


@pytest.mark.unit
async def test_redis_result_cache_without_package_raises_structured_error() -> None:
    cache = RedisResultCache()
    with pytest.raises(CacheBackendError) as excinfo:
        await cache.lookup("t", "v1", "k")
    assert excinfo.value.context["missing_package"] == "redis"


class _FakePgPool:
    def __init__(self) -> None:
        self.executed: list[str] = []
        self.row: dict[str, Any] | None = None

    async def execute(self, sql: str, *args: Any) -> None:
        self.executed.append(sql)

    async def fetchrow(self, sql: str, *args: Any) -> dict[str, Any] | None:
        self.executed.append(sql)
        return self.row


@pytest.mark.unit
async def test_postgres_result_cache_sql_shapes() -> None:
    pool = _FakePgPool()
    cache = PostgresResultCache(pool=pool)
    await cache.store("catalog/t", "v1", "k", EchoOut(echoed="x"), ttl_s=60)
    assert any("INSERT INTO foundry_tool_result_cache" in s for s in pool.executed)
    pool.row = {"output": {"echoed": "x"}, "cached_at": datetime.now(UTC)}
    hit = await cache.lookup("catalog/t", "v1", "k")
    assert hit is not None and hit.output == {"echoed": "x"}
    assert any("expires_at > now()" in s for s in pool.executed)


# --- dispatcher integration (cache steps inside ToolRegistry.dispatch) ---------------


class _Emitted:
    def __init__(self) -> None:
        self.events: list[tuple[type, dict[str, Any]]] = []

    def __call__(self, event_cls: type, **fields: Any) -> None:
        self.events.append((event_cls, fields))

    def kinds(self) -> list[type]:
        return [cls for cls, _ in self.events]


def _registry(handler: Any, *, cacheable: bool = True) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            descriptor=ToolDescriptor(
                name="echo", ref="local/echo", version="v1", description=""
            ),
            input_schema=EchoIn,
            output_schema=EchoOut,
            handler=handler,
            timeout_s=5.0,
            cacheable=cacheable,
            cache_ttl_s=60 if cacheable else None,
            cache_scope="project",
        )
    )
    return registry


def _ctx(cache: Any) -> RunContext:
    session = Session.new(project="unit", cache=CacheBundle(tool_result=cache))
    return RunContext(
        run_id="R" * 26,
        agent_name="tester",
        session=session,
        tool_ref="local/echo@v1",
        retry_policy=RetryPolicy(initial_delay_s=0.01, max_delay_s=0.02),
    )


@pytest.mark.unit
async def test_dispatch_second_identical_call_hits_cache_and_skips_handler() -> None:
    calls = 0

    async def handler(inputs: EchoIn, ctx: RunContext) -> EchoOut:
        nonlocal calls
        calls += 1
        return EchoOut(echoed=inputs.text)

    registry = _registry(handler)
    cache = InProcessResultCache(":memory:")
    emitted = _Emitted()
    ctx = _ctx(cache)

    out1 = await registry.dispatch("echo", ["echo"], {"text": "hi"}, ctx, emitted)
    out2 = await registry.dispatch("echo", ["echo"], {"text": "hi"}, ctx, emitted)
    assert calls == 1  # handler ran once; second call served from cache
    assert out1 == out2
    assert emitted.kinds() == [
        ToolCacheMiss, ToolStarted, ToolCompleted, ToolCacheStore,  # call 1
        ToolCacheHit,                                               # call 2
    ]
    # different input → miss again
    await registry.dispatch("echo", ["echo"], {"text": "other"}, ctx, emitted)
    assert calls == 2


@pytest.mark.unit
async def test_non_cacheable_tool_never_touches_the_cache() -> None:
    async def handler(inputs: EchoIn, ctx: RunContext) -> EchoOut:
        return EchoOut(echoed=inputs.text)

    registry = _registry(handler, cacheable=False)
    emitted = _Emitted()
    await registry.dispatch(
        "echo", ["echo"], {"text": "hi"}, _ctx(InProcessResultCache(":memory:")),
        emitted,
    )
    assert ToolCacheMiss not in emitted.kinds()
    assert ToolCacheStore not in emitted.kinds()


class _BrokenCache:
    async def lookup(self, *args: Any) -> CachedToolResult | None:
        raise CacheBackendError("backend down")

    async def store(self, *args: Any, **kwargs: Any) -> None:
        raise CacheBackendError("backend down")


@pytest.mark.unit
async def test_cache_failure_fails_open_with_warning_events() -> None:
    calls = 0

    async def handler(inputs: EchoIn, ctx: RunContext) -> EchoOut:
        nonlocal calls
        calls += 1
        return EchoOut(echoed=inputs.text)

    registry = _registry(handler)
    emitted = _Emitted()
    out = await registry.dispatch(
        "echo", ["echo"], {"text": "hi"}, _ctx(_BrokenCache()), emitted
    )
    assert out.echoed == "hi" and calls == 1  # never blocked the call
    warnings = [f for cls, f in emitted.events if cls is WarningEvent]
    assert len(warnings) == 2  # lookup fail-open + store fail-open
    assert all(w["category"] == "cache.tool.error" for w in warnings)


class _StaleSchemaCache:
    """Returns an entry that no longer validates against the output schema."""

    async def lookup(self, *args: Any) -> CachedToolResult:
        return CachedToolResult(
            output={"wrong_field": 1}, cached_at=datetime.now(UTC)
        )

    async def store(self, *args: Any, **kwargs: Any) -> None:
        pass


@pytest.mark.unit
async def test_corrupted_cached_output_is_a_miss_with_warning() -> None:
    async def handler(inputs: EchoIn, ctx: RunContext) -> EchoOut:
        return EchoOut(echoed=inputs.text)

    registry = _registry(handler)
    emitted = _Emitted()
    out = await registry.dispatch(
        "echo", ["echo"], {"text": "hi"}, _ctx(_StaleSchemaCache()), emitted
    )
    assert out.echoed == "hi"  # handler ran; corrupt entry ignored
    warnings = [f for cls, f in emitted.events if cls is WarningEvent]
    assert any(w["category"] == "cache.tool.corrupted_entry" for w in warnings)
