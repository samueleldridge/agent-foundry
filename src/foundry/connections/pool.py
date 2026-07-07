"""In-process ConnectionPool + the per-tool-call slot accessor (docs/23).

Pooling semantics (normative, docs/23 § Pooling semantics):
1. Pool keys are (ref, config_hash, project).
2. Concurrent acquires for a cold key coalesce on one factory build.
3. PoolPolicy.max_concurrent caps concurrent checkouts per entry;
   exceeding acquire_timeout_s raises ConnectionPoolExhausted.
4. Idle eviction is on-demand in 2a (no background task yet).
5. close_all() awaits every close with a per-connection timeout.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, ConfigDict

from foundry.connections.registry import PreparedConnection
from foundry.core.connection import (
    Connection,
    ConnectionContext,
    ConnectionDescriptor,
    ConnectionFactory,
    ConnectionHealth,
)
from foundry.core.errors import (
    ConnectionConfigError,
    ConnectionPoolExhausted,
    ConnectionSlotNotDeclaredError,
    FoundryError,
)
from foundry.core.events import ConnectionEvent
from foundry.core.tool import EmitFn

_CLOSE_TIMEOUT_S = 10.0

PoolKey = tuple[str, str, str]
"""(canonical ref, config_hash, project)."""


class PoolMetrics(BaseModel):
    """Counters surfaced in run artifacts — the exit-gate evidence that two
    tool calls sharing a slot reuse one client (1 build, N-1 cache hits)."""

    model_config = ConfigDict(extra="forbid")

    acquires: int = 0
    cache_hits: int = 0
    builds: int = 0
    evictions: int = 0
    refreshes: int = 0
    releases: int = 0


@dataclass
class _PoolEntry:
    connection: Connection[Any]
    semaphore: asyncio.Semaphore
    built_at: float
    last_acquired_at: float


@dataclass
class _KeyState:
    build_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class InProcessConnectionPool:
    """Per-process connection cache implementing the ConnectionPool protocol."""

    def __init__(self) -> None:
        self._entries: dict[PoolKey, _PoolEntry] = {}
        self._key_state: dict[PoolKey, _KeyState] = {}
        self._by_conn_id: dict[int, PoolKey] = {}
        self.metrics = PoolMetrics()

    def _state_for(self, key: PoolKey) -> _KeyState:
        state = self._key_state.get(key)
        if state is None:
            state = self._key_state[key] = _KeyState()
        return state

    async def acquire(
        self,
        ref: str,
        config_hash: str,
        project: str,
        factory: ConnectionFactory,
        factory_args: Any,
        *,
        max_concurrent: int = 32,
        acquire_timeout_s: float = 30.0,
    ) -> Connection[Any]:
        key: PoolKey = (ref, config_hash, project)
        self.metrics.acquires += 1

        state = self._state_for(key)
        async with state.build_lock:  # coalesce cold-cache builds
            entry = self._entries.get(key)
            if entry is None:
                entry = await self._build(key, factory, factory_args, max_concurrent)
            else:
                self.metrics.cache_hits += 1

        try:
            async with asyncio.timeout(acquire_timeout_s):
                await entry.semaphore.acquire()
        except TimeoutError as exc:
            raise ConnectionPoolExhausted(
                f"connection {ref!r} reached its max_concurrent cap "
                f"({max_concurrent}); no slot freed within "
                f"{acquire_timeout_s}s",
                context={"ref": ref, "project": project,
                         "max_concurrent": max_concurrent,
                         "acquire_timeout_s": acquire_timeout_s},
                cause=exc,
            ) from exc
        entry.last_acquired_at = time.time()
        return entry.connection

    async def _build(
        self,
        key: PoolKey,
        factory: ConnectionFactory,
        factory_args: Any,
        max_concurrent: int,
    ) -> _PoolEntry:
        ref, _, _ = key
        try:
            connection = await factory(
                factory_args.config, factory_args.credentials, factory_args.ctx
            )
        except FoundryError:
            raise  # ConnectionAuthError et al. pass through classified
        except Exception as exc:
            raise ConnectionConfigError(
                f"connection factory for {ref!r} raised "
                f"{type(exc).__name__}: {exc}",
                context={"ref": ref, "cause_type": type(exc).__name__},
                cause=exc,
            ) from exc
        entry = _PoolEntry(
            connection=connection,
            semaphore=asyncio.Semaphore(max_concurrent),
            built_at=time.time(),
            last_acquired_at=time.time(),
        )
        self._entries[key] = entry
        self._by_conn_id[id(connection)] = key
        self.metrics.builds += 1
        return entry

    async def release(self, conn: Connection[Any]) -> None:
        key = self._by_conn_id.get(id(conn))
        if key is None:
            return  # already evicted; nothing to return
        entry = self._entries.get(key)
        if entry is not None and entry.connection is conn:
            entry.semaphore.release()
            self.metrics.releases += 1

    async def refresh(self, ref: str, project: str) -> None:
        self.metrics.refreshes += 1
        await self.evict(ref, project)

    async def evict(self, ref: str, project: str | None = None) -> None:
        keys = [
            key
            for key in list(self._entries)
            if key[0] == ref and (project is None or key[2] == project)
        ]
        for key in keys:
            entry = self._entries.pop(key)
            self._by_conn_id.pop(id(entry.connection), None)
            self.metrics.evictions += 1
            await self._close_quietly(entry.connection)

    async def close_all(self) -> None:
        for key in list(self._entries):
            entry = self._entries.pop(key)
            self._by_conn_id.pop(id(entry.connection), None)
            await self._close_quietly(entry.connection)

    async def _close_quietly(self, conn: Connection[Any]) -> None:
        close = getattr(conn, "close", None)
        if close is None:
            return
        try:
            async with asyncio.timeout(_CLOSE_TIMEOUT_S):
                await close()
        except Exception:
            pass

    def metrics_snapshot(self) -> dict[str, Any]:
        return {
            **self.metrics.model_dump(),
            "entries": [
                {"ref": ref, "config_hash": cfg, "project": project}
                for (ref, cfg, project) in self._entries
            ],
        }


@dataclass(frozen=True)
class _FactoryArgs:
    config: Any
    credentials: Any
    ctx: ConnectionContext


class SlotConnectionAccessor:
    """The ConnectionAccessor a tool handler sees: slot names in, pooled
    authenticated connections out. One accessor per tool call."""

    def __init__(
        self,
        pool: InProcessConnectionPool,
        project: str,
        slots: dict[str, PreparedConnection],
        ctx: ConnectionContext,
        *,
        agent_name: str = "",
        emit: EmitFn | None = None,
    ) -> None:
        self._pool = pool
        self._project = project
        self._slots = slots
        self._ctx = ctx
        self._agent_name = agent_name
        self._emit = emit
        self._acquired: dict[str, Connection[Any]] = {}

    def _prepared(self, slot: str) -> PreparedConnection:
        prepared = self._slots.get(slot)
        if prepared is None:
            raise ConnectionSlotNotDeclaredError(
                f"tool requested connection slot {slot!r}, which it did not "
                f"declare in connections_required (declared: "
                f"{', '.join(sorted(self._slots)) or '(none)'})",
                context={"slot": slot, "declared_slots": sorted(self._slots)},
            )
        return prepared

    async def get(self, slot: str) -> Connection[Any]:
        prepared = self._prepared(slot)
        started = time.monotonic()
        builds_before = self._pool.metrics.builds
        conn = await self._pool.acquire(
            prepared.canonical_ref,
            prepared.config_hash,
            self._project,
            prepared.loaded.factory,
            _FactoryArgs(
                config=prepared.config,
                credentials=prepared.credentials,
                ctx=self._ctx,
            ),
            max_concurrent=prepared.pool_policy.max_concurrent,
            acquire_timeout_s=prepared.pool_policy.acquire_timeout_s,
        )
        self._acquired[slot] = conn
        if self._emit is not None:
            lifecycle = (
                "acquire" if self._pool.metrics.builds > builds_before else "cache_hit"
            )
            self._emit(
                ConnectionEvent,
                agent_name=self._agent_name,
                connection_descriptor=self.descriptor(slot),
                lifecycle=lifecycle,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        return conn

    async def health(self, slot: str) -> ConnectionHealth:
        conn = await self.get(slot)
        return await conn.health()

    def descriptor(self, slot: str) -> ConnectionDescriptor:
        prepared = self._prepared(slot)
        return prepared.descriptor.model_copy(update={"slot": slot})

    async def on_auth_error(self) -> bool:
        """Evict pool entries for acquired slots whose refresh policy is
        on_auth_error. True → the dispatcher retries the handler once."""
        evicted = False
        for slot, _conn in list(self._acquired.items()):
            prepared = self._slots[slot]
            if prepared.refresh.mode != "on_auth_error":
                continue
            await self._pool.evict(prepared.canonical_ref, self._project)
            self._acquired.pop(slot, None)
            evicted = True
            if self._emit is not None:
                self._emit(
                    ConnectionEvent,
                    agent_name=self._agent_name,
                    connection_descriptor=self.descriptor(slot),
                    lifecycle="evict",
                )
        return evicted

    async def release_all(self) -> None:
        for slot, conn in list(self._acquired.items()):
            await self._pool.release(conn)
            self._acquired.pop(slot, None)


__all__ = [
    "InProcessConnectionPool",
    "PoolKey",
    "PoolMetrics",
    "SlotConnectionAccessor",
]
