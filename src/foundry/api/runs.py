"""RunManager — live-run registry, event broadcast, cancellation, resume.

One RunManager per served project per worker process. It owns:

- **starting runs**: each run drives ``foundry.runtime.run_project`` as a
  child task of the app-lifespan task group (structured concurrency —
  docs/71: task groups for all fan-out; no orphan tasks at shutdown);
- **event broadcast**: the run's ``event_sink`` appends to the in-memory
  history, persists through the ``RunArtifactWriter`` (the SSE-replay
  substrate), and fans out to every subscriber queue — SSE responses,
  WebSocket pumps, and the batch executor all subscribe here;
- **HITL pauses**: ``run_project`` returning ``approval_pending`` parks
  the drive loop on an inbox until an ``ApprovalResponse`` arrives (WS
  frame or ``POST /runs/{id}/resume``), then continues the SAME event
  sequence (docs/32 § Resume);
- **cancellation** (docs/71 § Cancellation): cooperative (the session's
  cancel token) + structural (a per-run ``CancelScope``); the checkpointer
  has persisted node-boundary state, a ``run.cancelled`` event with the
  typed reason closes the stream, and the run can be resumed later;
- **timeouts**: ``Guardrails.max_wall_time_s`` enforced with
  ``anyio.move_on_after`` → ``run.cancelled(reason="timeout")``;
- **graceful drain** (docs/71 § Graceful shutdown): mark draining →
  wait for in-flight runs up to the drain timeout → force-cancel the
  rest with ``reason="worker_drain"`` (checkpoints survive).

Metadata mirrors the CLI writer's shapes so ``foundry resume`` /
``foundry approvals list`` see API-initiated runs unchanged.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import anyio
from pydantic import BaseModel

from foundry.api.schemas import PendingApproval, RunStatusResponse
from foundry.api.worker import WorkerState
from foundry.core import (
    CostBudget,
    RunCancelledEvent,
    RunId,
    Session,
)
from foundry.core.errors import FoundryError, OrchestrationError
from foundry.observability.artifacts import RunArtifactWriter
from foundry.observability.logging import run_logger
from foundry.runtime.compiled import CompiledProject
from foundry.runtime.langgraph_adapter import run_project
from foundry.storage.paths import run_dir

TERMINAL_EVENTS = frozenset({"run.completed", "run.failed", "run.cancelled"})

_SENTINEL: Any = None
"""Queue sentinel signalling 'no more events for this run'."""


class LiveRun:
    """One run's in-process record: status, event history, subscribers."""

    def __init__(
        self,
        run_id: RunId,
        project: str,
        writer: RunArtifactWriter,
        session: Session,
        start_sequence: int,
    ) -> None:
        self.run_id = run_id
        self.project = project
        self.writer = writer
        self.session = session
        self.base_sequence = start_sequence
        self.events: list[dict[str, Any]] = []
        """This process's events, contiguous from base_sequence. Earlier
        sequences (a resumed run) live in the artifact only."""
        self.subscribers: set[asyncio.Queue[Any]] = set()
        self.status = "in_progress"
        self.output: Any = None
        self.error: dict[str, Any] | None = None
        self.pending_approval: dict[str, Any] | None = None
        self.inbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        """Approval responses ({approval_id, decision, reason})."""
        self.done = asyncio.Event()
        self.attention = asyncio.Event()
        """Pulsed whenever the run reaches a decision point a non-streaming
        caller waits on: terminal state OR approval_pending pause."""
        self.scope: anyio.CancelScope | None = None
        self.cancel_reason: str | None = None
        self.started_at = datetime.now(UTC)
        self.completed_at: datetime | None = None
        self.current_node: str | None = None
        self.tokens_used = 0
        self.cost_so_far_usd = Decimal("0")

    # --- event flow --------------------------------------------------------------

    def next_sequence(self) -> int:
        return self.base_sequence + len(self.events)

    def sink(self, event: BaseModel) -> None:
        """The run_project event_sink: persist → mirror → fan out."""
        self.writer.record_event(event)
        data: dict[str, Any] = json.loads(event.model_dump_json())
        self.events.append(data)
        name = data.get("event", "")
        if name == "agent.started":
            self.current_node = data.get("agent_name")
        elif name == "llm.completed":
            usage = data.get("usage") or {}
            self.tokens_used += int(usage.get("input_tokens", 0)) + int(
                usage.get("output_tokens", 0)
            )
            cost = data.get("cost_estimate_usd")
            if cost is not None:
                self.cost_so_far_usd += Decimal(str(cost))
        for queue in list(self.subscribers):
            queue.put_nowait(data)

    def broadcast_close(self) -> None:
        for queue in list(self.subscribers):
            queue.put_nowait(_SENTINEL)

    def event_at(self, sequence: int) -> dict[str, Any] | None:
        index = sequence - self.base_sequence
        if 0 <= index < len(self.events):
            return self.events[index]
        return None

    @property
    def active(self) -> bool:
        return not self.done.is_set()

    def status_response(self, system_version: str) -> RunStatusResponse:
        return RunStatusResponse(
            run_id=str(self.run_id),
            project=self.project,
            system_version=system_version,
            status=self.status,
            started_at=self.started_at,
            completed_at=self.completed_at,
            current_node=self.current_node if self.active else None,
            tokens_used=self.tokens_used,
            cost_so_far_usd=(
                str(self.cost_so_far_usd) if self.cost_so_far_usd else None
            ),
            pending_approval=(
                PendingApproval.model_validate(self.pending_approval)
                if self.pending_approval
                else None
            ),
            error=self.error,
            events_url=f"/runs/{self.run_id}/events",
        )


class RunManager:
    """Live-run registry + drive loop for one served project."""

    def __init__(
        self,
        compiled: CompiledProject,
        *,
        worker_state: WorkerState | None = None,
        checkpoint: str = "sqlite",
        checkpoint_db: Path | None = None,
        max_concurrent_runs: int | None = None,
        drain_timeout_s: float | None = None,
    ) -> None:
        self.compiled = compiled
        self.project = compiled.project.system.name
        self.worker_state = worker_state or WorkerState()
        self.checkpoint = checkpoint
        self.checkpoint_db = checkpoint_db
        self.max_concurrent_runs = max_concurrent_runs or int(
            os.environ.get("FOUNDRY_MAX_CONCURRENT_RUNS", "100")
        )
        self.drain_timeout_s = (
            drain_timeout_s
            if drain_timeout_s is not None
            else float(os.environ.get("FOUNDRY_DRAIN_TIMEOUT_S", "120"))
        )
        self._runs: dict[str, LiveRun] = {}
        self._tg: Any = None

    # --- lifecycle ---------------------------------------------------------------

    def bind(self, task_group: Any) -> None:
        """Attach the app-lifespan task group; every run drives inside it
        (structured concurrency: shutdown cancels nothing silently)."""
        self._tg = task_group

    def active_count(self) -> int:
        return sum(1 for live in self._runs.values() if live.active)

    def can_accept(self) -> bool:
        return (
            self._tg is not None
            and not self.worker_state.draining
            and self.active_count() < self.max_concurrent_runs
        )

    async def shutdown(self) -> None:
        """docs/71 § Graceful shutdown: drain, then force-cancel."""
        self.worker_state.draining = True
        active = [live for live in self._runs.values() if live.active]
        with anyio.move_on_after(self.drain_timeout_s):
            for live in active:
                await live.done.wait()
        for live in active:
            if live.active:
                self.cancel(str(live.run_id), "worker_drain")
        with anyio.move_on_after(5.0):
            for live in active:
                await live.done.wait()

    # --- starting / resuming runs ---------------------------------------------------

    def _new_live(self, run_id: RunId | None = None) -> LiveRun:
        rid = run_id or RunId.new()
        writer = RunArtifactWriter(rid)
        guardrails = self.compiled.project.system.guardrails
        session = Session.new(
            project=self.project,
            run_id=rid,
            cost_budget=(
                CostBudget(max_usd=guardrails.max_cost_usd)
                if guardrails.max_cost_usd is not None
                else None
            ),
            logger=run_logger(str(rid)),
            system_version=self.compiled.system_version,
            pin_set_hash=self.compiled.pin_set_hash,
        )
        return LiveRun(
            rid, self.project, writer, session, writer.next_sequence()
        )

    def _require_tg(self) -> Any:
        if self._tg is None:
            raise OrchestrationError(
                "RunManager is not bound to a task group — the app lifespan "
                "has not started (tests: run the ASGI app's lifespan, e.g. "
                "TestClient(app) as a context manager)",
                context={"project": self.project},
            )
        return self._tg

    def spawn(self, fn: Any, *args: Any) -> None:
        """Run a coroutine as a child of the app-lifespan task group
        (structured concurrency for API-layer background work — the batch
        executor rides here)."""
        self._require_tg().start_soon(fn, *args)

    def start_run(
        self, input_data: dict[str, Any], *, run_id: RunId | None = None
    ) -> LiveRun:
        live = self._new_live(run_id)
        self._runs[str(live.run_id)] = live
        self._require_tg().start_soon(self._drive, live, input_data, None)
        self._prune()
        return live

    def resume_run(self, run_id: str) -> LiveRun:
        """Re-drive an interrupted (cancelled / paused / killed) run from
        its checkpoint. Fresh session, same run_id; sequences continue."""
        live = self._new_live(RunId.validate(run_id))
        self._runs[str(live.run_id)] = live
        self._require_tg().start_soon(self._drive, live, {}, None)
        return live

    def start_approval_resume(
        self, run_id: str, approval: dict[str, Any]
    ) -> LiveRun:
        """Resume an approval-pending run that is NOT live in this process
        (server restarted or the pause outlived the connection): fresh
        drive with the approval response threaded in (docs/32)."""
        live = self._new_live(RunId.validate(run_id))
        self._runs[str(live.run_id)] = live
        self._require_tg().start_soon(self._drive, live, {}, approval)
        return live

    _MAX_RETAINED_DONE = 512

    def _prune(self) -> None:
        """Cap the in-memory registry: completed runs stay queryable via
        their artifacts; only the most recent done LiveRuns are retained
        (sustained load must not grow memory unboundedly)."""
        done = [
            key for key, live in self._runs.items() if not live.active
        ]
        for key in done[: max(0, len(done) - self._MAX_RETAINED_DONE)]:
            del self._runs[key]

    # --- inbound control ---------------------------------------------------------

    def get(self, run_id: str) -> LiveRun | None:
        return self._runs.get(run_id)

    def cancel(self, run_id: str, reason: str) -> bool:
        """Cooperative + structural cancel. True if a live run was hit."""
        live = self._runs.get(run_id)
        if live is None or not live.active:
            return False
        live.cancel_reason = reason
        live.session.cancel_token.cancel(reason)
        if live.status == "approval_pending":
            # The drive loop is parked on the inbox, not inside the graph.
            live.inbox.put_nowait({"__cancelled__": reason})
        if live.scope is not None:
            live.scope.cancel()
        return True

    def deliver_approval(
        self, run_id: str, approval: dict[str, Any]
    ) -> LiveRun:
        live = self._runs.get(run_id)
        if live is not None and live.active:
            if live.status != "approval_pending":
                raise OrchestrationError(
                    f"run {run_id} is {live.status}, not approval_pending — "
                    "nothing to approve",
                    context={"run_id": run_id, "status": live.status},
                )
            live.inbox.put_nowait(approval)
            return live
        # Not live: resume from the artifact + checkpoint.
        metadata = self.read_artifact_metadata(run_id)
        if metadata is None or metadata.get("status") != "approval_pending":
            raise OrchestrationError(
                f"run {run_id} has no pending approval on record",
                context={
                    "run_id": run_id,
                    "status": (metadata or {}).get("status", "unknown"),
                },
            )
        if metadata.get("project") != self.project:
            raise OrchestrationError(
                f"run {run_id} belongs to project "
                f"{metadata.get('project')!r}, not {self.project!r}",
                context={"run_id": run_id},
            )
        return self.start_approval_resume(run_id, approval)

    # --- the drive loop ---------------------------------------------------------------

    async def _drive(
        self,
        live: LiveRun,
        input_data: dict[str, Any],
        approval_response: dict[str, Any] | None,
    ) -> None:
        guardrails = self.compiled.project.system.guardrails
        wall_s = guardrails.max_wall_time_s or math.inf
        failed: FoundryError | None = None
        result = None
        try:
            with anyio.CancelScope() as hard_scope:
                live.scope = hard_scope
                with anyio.move_on_after(wall_s) as wall_scope:
                    response = approval_response
                    while True:
                        result = await run_project(
                            self.compiled,
                            input_data,
                            live.session,
                            live.sink,
                            checkpointer=self.checkpoint,
                            checkpoint_db=self.checkpoint_db,
                            start_sequence=live.next_sequence(),
                            approval_response=response,
                        )
                        if result.status != "approval_pending":
                            break
                        live.status = "approval_pending"
                        live.pending_approval = result.pending_approval
                        self._write_pause_metadata(live)
                        live.attention.set()
                        inbound = await live.inbox.get()
                        if "__cancelled__" in inbound:
                            live.cancel_reason = str(
                                inbound["__cancelled__"]
                            )
                            hard_scope.cancel()
                            await anyio.sleep_forever()  # pragma: no cover
                        response = inbound
                        live.status = "in_progress"
                        live.pending_approval = None
            if hard_scope.cancelled_caught:
                self._finish_cancelled(
                    live, live.cancel_reason or "user_abort"
                )
                return
            if wall_scope.cancelled_caught:
                live.session.cancel_token.cancel("timeout")
                self._finish_cancelled(live, "timeout")
                return
        except FoundryError as exc:
            failed = exc
        except BaseException:
            # Task-level cancellation from the lifespan group tearing down
            # (hostile shutdown): checkpoint state is already durable.
            self._finish_cancelled(
                live, live.cancel_reason or "worker_drain"
            )
            raise
        if failed is not None:
            live.status = "failed"
            live.error = failed.to_dict()
            live.completed_at = datetime.now(UTC)
            live.writer.write_metadata(
                project=self.project,
                status="failed",
                provider=self.compiled.provider.name,
                model=self.compiled.provider.model,
                error=live.error,
                extra=self._metadata_extra(),
            )
            live.broadcast_close()
            live.done.set()
            live.attention.set()
            return
        assert result is not None
        live.status = "completed"
        live.output = result.output
        live.completed_at = datetime.now(UTC)
        if result.final_state is not None:
            live.writer.write_final_state(result.final_state)
        live.writer.write_metadata(
            project=self.project,
            status="completed",
            provider=self.compiled.provider.name,
            model=self.compiled.provider.model,
            extra={
                "output": result.output,
                "run_status": result.status,
                "resumed": result.resumed,
                "llm_call_count": result.llm_call_count,
                "connection_pool": result.pool_metrics,
                **self._metadata_extra(),
            },
        )
        live.broadcast_close()
        live.done.set()
        live.attention.set()

    def _finish_cancelled(self, live: LiveRun, reason: str) -> None:
        """Emit run.cancelled with the typed reason (docs/71 § Standard
        cancellation reasons), persist resumable metadata, close streams.
        The checkpointer already holds the last node-boundary state."""
        status = "paused" if reason == "pause" else "cancelled"
        live.status = status
        live.cancel_reason = reason
        live.completed_at = datetime.now(UTC)
        event = RunCancelledEvent(
            run_id=live.run_id,
            sequence=live.next_sequence(),
            timestamp=datetime.now(UTC),
            worker_id=self.worker_state.worker_id,
            reason=reason,
        )
        live.sink(event)
        live.writer.write_metadata(
            project=self.project,
            status=status,
            provider=self.compiled.provider.name,
            model=self.compiled.provider.model,
            extra={"cancel_reason": reason, **self._metadata_extra()},
        )
        live.broadcast_close()
        live.done.set()
        live.attention.set()

    def _write_pause_metadata(self, live: LiveRun) -> None:
        """Mirror cli/run.py's approval_pending artifact shape so
        `foundry resume` / `foundry approvals list` work on API runs."""
        live.writer.write_metadata(
            project=self.project,
            status="approval_pending",
            provider=self.compiled.provider.name,
            model=self.compiled.provider.model,
            extra={
                "pending_approval": live.pending_approval,
                **self._metadata_extra(),
            },
        )

    def _metadata_extra(self) -> dict[str, Any]:
        return {
            "pins": self.compiled.pins,
            "checkpointer": self.checkpoint,
            "project_path": str(self.compiled.project.directory.resolve()),
            "worker_id": self.worker_state.worker_id,
        }

    # --- status + replay surface ----------------------------------------------------

    def read_artifact_metadata(self, run_id: str) -> dict[str, Any] | None:
        path = run_dir(run_id) / "metadata.json"
        if not path.is_file():
            return None
        try:
            loaded = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        return loaded if isinstance(loaded, dict) else None

    def read_artifact_events(
        self, run_id: str, from_sequence: int = -1
    ) -> list[dict[str, Any]]:
        """Persisted RunEvent replay: complete events.jsonl lines with
        sequence > from_sequence (docs/70 § Reconnect via Last-Event-ID)."""
        path = run_dir(run_id) / "events.jsonl"
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        raw = path.read_bytes()
        complete, _, _partial = raw.rpartition(b"\n")
        for line in complete.splitlines():
            if not line.strip():
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict) and data.get("sequence", -1) > from_sequence:
                events.append(data)
        return events

    def run_status(self, run_id: str) -> RunStatusResponse | None:
        live = self._runs.get(run_id)
        if live is not None:
            return live.status_response(self.compiled.system_version)
        metadata = self.read_artifact_metadata(run_id)
        if metadata is None:
            return None
        pending = metadata.get("pending_approval")
        return RunStatusResponse(
            run_id=run_id,
            project=str(metadata.get("project", self.project)),
            system_version=self.compiled.system_version,
            status=str(metadata.get("status", "unknown")),
            pending_approval=(
                PendingApproval.model_validate(pending) if pending else None
            ),
            error=metadata.get("error"),
            events_url=f"/runs/{run_id}/events",
        )


__all__ = ["TERMINAL_EVENTS", "LiveRun", "RunManager"]
