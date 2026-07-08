"""ConnectionPool semantics + slot accessor (docs/23 § Pooling semantics)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from foundry.connections.pool import InProcessConnectionPool
from foundry.core.connection import ConnectionHealth
from foundry.core.errors import ConnectionConfigError, ConnectionPoolExhausted


class FakeConnection:
    def __init__(self, ident: int) -> None:
        self.ref = "catalog/fake@v1"
        self.slot = ""
        self.ident = ident
        self.closed = False

    @property
    def client(self) -> int:
        return self.ident

    async def health(self) -> ConnectionHealth:
        return ConnectionHealth(ok=True, checked_at=datetime.now(UTC))

    async def close(self) -> None:
        self.closed = True


@dataclass
class FakeFactory:
    delay_s: float = 0.0
    builds: int = 0
    fail_with: Exception | None = None

    async def __call__(self, config: Any, credentials: Any, ctx: Any) -> FakeConnection:
        if self.delay_s:
            await asyncio.sleep(self.delay_s)
        if self.fail_with is not None:
            raise self.fail_with
        self.builds += 1
        return FakeConnection(self.builds)


@dataclass(frozen=True)
class Args:
    config: Any = None
    credentials: Any = None
    ctx: Any = None


@pytest.mark.unit
async def test_second_acquire_is_a_cache_hit_same_instance() -> None:
    pool = InProcessConnectionPool()
    factory = FakeFactory()
    first = await pool.acquire("catalog/fake@v1", "h", "proj", factory, Args())
    second = await pool.acquire("catalog/fake@v1", "h", "proj", factory, Args())
    assert first is second
    assert factory.builds == 1
    assert pool.metrics.acquires == 2
    assert pool.metrics.cache_hits == 1
    assert pool.metrics.builds == 1


@pytest.mark.unit
async def test_concurrent_cold_acquires_coalesce_on_one_build() -> None:
    pool = InProcessConnectionPool()
    factory = FakeFactory(delay_s=0.05)
    results = await asyncio.gather(
        *(
            pool.acquire("catalog/fake@v1", "h", "proj", factory, Args())
            for _ in range(5)
        )
    )
    assert factory.builds == 1
    assert all(conn is results[0] for conn in results)


@pytest.mark.unit
async def test_pool_keys_isolate_config_and_project() -> None:
    pool = InProcessConnectionPool()
    factory = FakeFactory()
    a = await pool.acquire("catalog/fake@v1", "h1", "proj", factory, Args())
    b = await pool.acquire("catalog/fake@v1", "h2", "proj", factory, Args())
    c = await pool.acquire("catalog/fake@v1", "h1", "other_proj", factory, Args())
    assert len({id(a), id(b), id(c)}) == 3
    assert factory.builds == 3


@pytest.mark.unit
async def test_evict_closes_and_next_acquire_rebuilds() -> None:
    pool = InProcessConnectionPool()
    factory = FakeFactory()
    first = await pool.acquire("catalog/fake@v1", "h", "proj", factory, Args())
    await pool.evict("catalog/fake@v1", "proj")
    assert first.closed
    assert pool.metrics.evictions == 1
    second = await pool.acquire("catalog/fake@v1", "h", "proj", factory, Args())
    assert second is not first
    assert factory.builds == 2


@pytest.mark.unit
async def test_max_concurrent_cap_raises_pool_exhausted() -> None:
    pool = InProcessConnectionPool()
    factory = FakeFactory()
    conn = await pool.acquire(
        "catalog/fake@v1", "h", "proj", factory, Args(),
        max_concurrent=1, acquire_timeout_s=0.05,
    )
    with pytest.raises(ConnectionPoolExhausted):
        await pool.acquire(
            "catalog/fake@v1", "h", "proj", factory, Args(),
            max_concurrent=1, acquire_timeout_s=0.05,
        )
    await pool.release(conn)
    again = await pool.acquire(
        "catalog/fake@v1", "h", "proj", factory, Args(),
        max_concurrent=1, acquire_timeout_s=0.05,
    )
    assert again is conn


@pytest.mark.unit
async def test_factory_exception_wrapped_as_connection_config_error() -> None:
    pool = InProcessConnectionPool()
    factory = FakeFactory(fail_with=RuntimeError("driver exploded"))
    with pytest.raises(ConnectionConfigError) as excinfo:
        await pool.acquire("catalog/fake@v1", "h", "proj", factory, Args())
    assert excinfo.value.context["cause_type"] == "RuntimeError"


@pytest.mark.unit
async def test_close_all_closes_every_entry() -> None:
    pool = InProcessConnectionPool()
    factory = FakeFactory()
    a = await pool.acquire("catalog/fake@v1", "h1", "proj", factory, Args())
    b = await pool.acquire("catalog/fake@v1", "h2", "proj", factory, Args())
    await pool.close_all()
    assert a.closed and b.closed
    assert pool.metrics_snapshot()["entries"] == []
