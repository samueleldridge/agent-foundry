"""POST /batch — batch submission primitive (docs/85 § Batch submission).

A batch item IS a run: each item goes through the normal RunManager path
(own run_id, own artifact, own checkpoint thread). Batch-level concerns
wrap it:

- **bounded parallelism** — a capacity semaphore caps in-flight items at
  ``policy.max_parallel`` (docs/71 § Backpressure via bounded task groups);
- **batch cost budget** — every ``llm.completed``'s ``cost_estimate_usd``
  accumulates into the batch counter; once ``policy.max_cost_usd`` is
  breached (``stop_on_budget_exceeded``), a ``batch.budget_exceeded``
  event is emitted BEFORE any cancellations it triggers (docs/85
  invariant 8), in-flight items run to completion, and not-yet-started
  items fast-fail with a synthetic ``run.cancelled(reason=
  "batch_budget_exceeded")``;
- **per-item timeout** — ``policy.per_item_timeout_s`` cancels the item's
  run with ``reason="timeout"``;
- **one SSE connection** — every per-item RunEvent is forwarded tagged
  with ``batch_id`` + ``item_id``; the stream ends with a single
  ``batch.completed`` summary. A client disconnect cancels the batch:
  in-flight item runs get ``run.cancelled(reason="user_abort")`` and
  their checkpoints persist.

The executor itself runs on the app-lifespan task group (structured
concurrency; the SSE generator only consumes a memory stream — no task
group suspended across a ``yield``). The budget counter is in-process: a
batch executes on the worker that accepted it. The docs/85 Redis counter
(multi-worker batch fan-out) is the documented scale-up path, not a v1
behaviour.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import anyio
from pydantic import BaseModel, ConfigDict, Field

from foundry.api.runs import TERMINAL_EVENTS, RunManager
from foundry.api.streaming import subscribe_events
from foundry.core import RunId


class BatchItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str
    input: dict[str, Any]


class BatchPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_parallel: int = Field(default=32, ge=1, le=1024)
    max_cost_usd: Decimal | None = None
    per_item_timeout_s: float = Field(default=300.0, gt=0)
    stop_on_budget_exceeded: bool = True
    stop_on_failure_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    streaming: bool = True
    """v1 supports the streaming (SSE) response shape only; the 202 +
    poll shape (docs/85) is deferred with the non-streaming store."""


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_id: str | None = None
    items: list[BatchItem] = Field(min_length=1)
    policy: BatchPolicy = Field(default_factory=BatchPolicy)

    def resolved_batch_id(self) -> str:
        return self.batch_id or str(RunId.new())


class _BatchState:
    def __init__(self, batch_id: str, policy: BatchPolicy) -> None:
        self.batch_id = batch_id
        self.policy = policy
        self.cost_usd = Decimal("0")
        self.budget_exceeded = False
        self.failure_tripped = False
        self.succeeded = 0
        self.failed = 0
        self.cancelled = 0
        self.completed_items = 0

    def over_budget(self) -> bool:
        return (
            self.policy.max_cost_usd is not None
            and self.cost_usd >= self.policy.max_cost_usd
        )

    def failure_rate_hit(self) -> bool:
        rate = self.policy.stop_on_failure_rate
        if rate is None or self.completed_items == 0:
            return False
        return (self.failed / self.completed_items) >= rate


def _tagged(data: dict[str, Any], batch_id: str, item_id: str) -> dict[str, Any]:
    """Per-item RunEvents carry batch_id + item_id on the wire (docs/85).
    The per-run artifact stays untagged; batch linkage is wire-level."""
    return {**data, "batch_id": batch_id, "item_id": item_id}


def _sse(data: dict[str, Any]) -> str:
    return (
        f"event: {data.get('event', 'message')}\n"
        f"data: {json.dumps(data, separators=(',', ':'), default=str)}\n\n"
    )


def _synthetic_cancelled(
    state: _BatchState, item_id: str, reason: str
) -> dict[str, Any]:
    """Fast-fail frame for an item whose run never started (or whose
    terminal event is being summarised on the batch stream)."""
    return {
        "event": "run.cancelled",
        "run_id": None,
        "sequence": 0,
        "timestamp": datetime.now(UTC).isoformat(),
        "reason": reason,
        "batch_id": state.batch_id,
        "item_id": item_id,
    }


class _ClientGone(Exception):
    """The SSE consumer closed its stream; abort the batch."""


class _BatchExecutor:
    def __init__(self, manager: RunManager, request: BatchRequest) -> None:
        self.manager = manager
        self.request = request
        self.state = _BatchState(request.resolved_batch_id(), request.policy)
        send, receive = anyio.create_memory_object_stream[
            dict[str, Any] | None
        ](max_buffer_size=float("inf"))
        self._send = send
        self.receive = receive
        self._inflight: set[str] = set()
        """run_ids of started-but-unfinished item runs (abort targets)."""
        self._aborted = False

    async def _emit(self, frame: dict[str, Any]) -> None:
        try:
            await self._send.send(frame)
        except (anyio.BrokenResourceError, anyio.ClosedResourceError) as exc:
            raise _ClientGone from exc

    async def _record_cost(self, data: dict[str, Any]) -> None:
        cost = data.get("cost_estimate_usd")
        if cost is None:
            return
        self.state.cost_usd += Decimal(str(cost))
        if (
            not self.state.budget_exceeded
            and self.state.over_budget()
            and self.request.policy.stop_on_budget_exceeded
        ):
            # docs/85 invariant 8: the breach event precedes any
            # cancellation it triggers.
            self.state.budget_exceeded = True
            await self._emit(
                {
                    "event": "batch.budget_exceeded",
                    "batch_id": self.state.batch_id,
                    "max_cost_usd": str(self.request.policy.max_cost_usd),
                    "cost_usd": str(self.state.cost_usd),
                }
            )

    async def _run_item(
        self, limiter: anyio.Semaphore, item: BatchItem
    ) -> None:
        state = self.state
        async with limiter:
            if self._aborted:
                # Client already gone: nobody is listening and abort()
                # has run — do not start a run that would leak.
                return
            if state.budget_exceeded or state.failure_tripped:
                reason = (
                    "batch_budget_exceeded"
                    if state.budget_exceeded
                    else "batch_failure_rate_tripped"
                )
                state.cancelled += 1
                state.completed_items += 1
                await self._emit(
                    _synthetic_cancelled(state, item.item_id, reason)
                )
                return
            # Per-item admission (Phase 9 pre-work): batch items are normal
            # runs and must respect the worker's max_concurrent_runs and
            # drain state — the batch's own semaphore does not exempt them.
            # At capacity we WAIT (bounded by the per-item timeout);
            # draining fast-fails the item resumably.
            admission_deadline = (
                time.monotonic() + self.request.policy.per_item_timeout_s
            )
            while not self.manager.can_accept():
                if self.manager.worker_state.draining:
                    state.cancelled += 1
                    state.completed_items += 1
                    await self._emit(
                        _synthetic_cancelled(
                            state, item.item_id, "worker_drain"
                        )
                    )
                    return
                if time.monotonic() >= admission_deadline:
                    state.cancelled += 1
                    state.completed_items += 1
                    await self._emit(
                        _synthetic_cancelled(state, item.item_id, "timeout")
                    )
                    return
                await anyio.sleep(0.02)
            live = self.manager.start_run(item.input)
            run_key = str(live.run_id)
            self._inflight.add(run_key)
            terminal: str | None = None
            try:
                with anyio.move_on_after(
                    self.request.policy.per_item_timeout_s
                ):
                    async for data in subscribe_events(
                        self.manager, run_key, live.base_sequence
                    ):
                        if data.get("event") == "llm.completed":
                            await self._record_cost(data)
                        await self._emit(
                            _tagged(data, state.batch_id, item.item_id)
                        )
                        if data.get("event") in TERMINAL_EVENTS:
                            terminal = str(data.get("event"))
                            break
            except _ClientGone:
                self.manager.cancel(run_key, "user_abort")
                raise
            finally:
                self._inflight.discard(run_key)
            if terminal is None:
                # Per-item timeout: cancel the run (checkpoint persists);
                # summarise on the batch stream.
                self.manager.cancel(run_key, "timeout")
                state.cancelled += 1
                await self._emit(
                    _synthetic_cancelled(state, item.item_id, "timeout")
                )
            elif terminal == "run.completed":
                state.succeeded += 1
            elif terminal == "run.failed":
                state.failed += 1
            else:
                state.cancelled += 1
            state.completed_items += 1
            if state.failure_rate_hit() and not state.failure_tripped:
                state.failure_tripped = True
                await self._emit(
                    {
                        "event": "batch.failure_rate_tripped",
                        "batch_id": state.batch_id,
                        "failed": state.failed,
                        "completed": state.completed_items,
                    }
                )

    async def abort(self) -> None:
        """Client-disconnect teardown (the module-docstring contract):
        cancel every started-but-unfinished item run with
        ``reason="user_abort"`` and wait for each to reach a terminal
        state. Items parked inside ``subscribe_events`` with no events
        flowing never observe the closed stream on their own, and the
        first item to catch ``_ClientGone`` task-cancels its siblings
        WITHOUT ``manager.cancel`` — so the teardown must hit the runs
        directly. The cancel loop is synchronous (no await), so every
        in-flight run is cancelled before any sibling task can unwind."""
        self._aborted = True
        run_ids = list(self._inflight)
        for run_id in run_ids:
            self.manager.cancel(run_id, "user_abort")
        with anyio.move_on_after(10.0):
            for run_id in run_ids:
                live = self.manager.get(run_id)
                if live is not None:
                    await live.done.wait()

    async def drive(self) -> None:
        """Runs on the app-lifespan task group (manager.spawn)."""
        limiter = anyio.Semaphore(self.request.policy.max_parallel)
        try:
            async with anyio.create_task_group() as tg:
                for item in self.request.items:
                    tg.start_soon(self._run_item, limiter, item)
        except BaseExceptionGroup as group:
            rest = group.split(_ClientGone)[1]
            if rest is not None:
                raise rest from None
        finally:
            # Synchronous close: the buffer is unbounded, so send_nowait
            # never blocks — and unlike ``await send(...)`` it offers no
            # checkpoint for a stale cancellation (the item-group
            # teardown after a client disconnect) to hijack, which would
            # skip the close() and leak the stream.
            try:
                self._send.send_nowait(None)
            except (
                anyio.BrokenResourceError,
                anyio.ClosedResourceError,
                anyio.WouldBlock,
            ):
                pass
            self._send.close()

    def summary(self, started_monotonic: float) -> dict[str, Any]:
        state = self.state
        return {
            "event": "batch.completed",
            "batch_id": state.batch_id,
            "total": len(self.request.items),
            "succeeded": state.succeeded,
            "failed": state.failed,
            "cancelled": state.cancelled,
            "total_cost_usd": str(state.cost_usd),
            "budget_exceeded": state.budget_exceeded,
            "duration_ms": int((time.monotonic() - started_monotonic) * 1000),
        }


async def execute_batch(
    manager: RunManager, request: BatchRequest
) -> AsyncIterator[str]:
    """The SSE body generator for POST /batch. The executor is a sibling
    task on the app nursery; this generator only consumes its stream, so
    closing the response mid-flight (client disconnect) tears the batch
    down through the stream, not through a suspended task group. The
    ``finally`` (starlette runs it on disconnect via ``aclose()``) closes
    the stream and then cancels every in-flight item run — the batch's
    disconnect contract: ``run.cancelled(reason=user_abort)``, checkpoints
    persist."""
    started = time.monotonic()
    executor = _BatchExecutor(manager, request)
    manager.spawn(executor.drive)
    try:
        async for frame in executor.receive:
            if frame is None:
                break
            yield _sse(frame)
        yield _sse(executor.summary(started))
    finally:
        executor.receive.close()
        await executor.abort()


__all__ = [
    "BatchItem",
    "BatchPolicy",
    "BatchRequest",
    "execute_batch",
]
