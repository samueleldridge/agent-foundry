"""Generic background-task supervision + progress SSE (docs/72 § Layouts,
tasks, health).

Anything long-running that is not forge or chat (evals, project tests,
storage gc) runs here as a child of the studio app's lifespan task group —
structured concurrency, no orphan work at shutdown. Each task owns an
:class:`EventLog` so ``GET /api/tasks/{id}/events`` streams progress with
Last-Event-ID resume.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anyio
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from foundry.core.errors import ConfigLoadError, FoundryError
from foundry.core.types import RunId
from foundry.studio.context import StudioContext
from foundry.studio.events import EventLog, resume_sequence, sse_log_stream
from foundry.studio.schemas import TaskInfo

TASK_TERMINAL_EVENTS = frozenset({"task.completed", "task.failed"})


@dataclass
class StudioTask:
    task_id: str
    kind: str
    status: str = "running"
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    result: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    log: EventLog = field(default_factory=EventLog)

    def info(self) -> TaskInfo:
        return TaskInfo(
            task_id=self.task_id,
            kind=self.kind,
            status=self.status,  # type: ignore[arg-type]
            created_at=self.created_at,
            result=self.result,
            error=self.error,
        )


class TaskRegistry:
    """task_id → StudioTask; work runs in the app lifespan task group."""

    def __init__(self, ctx: StudioContext) -> None:
        self._ctx = ctx
        self._tasks: dict[str, StudioTask] = {}

    def get(self, task_id: str) -> StudioTask:
        task = self._tasks.get(task_id)
        if task is None:
            raise ConfigLoadError(
                f"task {task_id!r} not found",
                context={"task_id": task_id, "not_found": True},
            )
        return task

    def launch(
        self, kind: str, work: Callable[[StudioTask], Awaitable[dict[str, Any]]]
    ) -> str:
        """Run an async job; its return dict becomes ``result``."""
        task = StudioTask(task_id=str(RunId.new()), kind=kind)
        self._tasks[task.task_id] = task
        task.log.append({"event": "task.started", "kind": kind})

        async def _drive() -> None:
            try:
                task.result = await work(task)
                task.status = "completed"
                task.log.append(
                    {"event": "task.completed", "result": task.result}
                )
            except FoundryError as exc:
                task.status = "failed"
                task.error = exc.to_dict()
                task.log.append({"event": "task.failed", "error": task.error})
            except Exception as exc:
                task.status = "failed"
                task.error = {
                    "error_class": type(exc).__name__,
                    "message": str(exc),
                    "context": {},
                }
                task.log.append({"event": "task.failed", "error": task.error})
            finally:
                task.log.close()

        self._ctx.spawn(_drive)
        return task.task_id

    def launch_sync(
        self, kind: str, work: Callable[[], dict[str, Any]]
    ) -> str:
        """Run a blocking job on a worker thread (pytest, gc, deploy)."""

        async def _threaded(_task: StudioTask) -> dict[str, Any]:
            return await anyio.to_thread.run_sync(work)

        return self.launch(kind, _threaded)


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.get("/tasks/{task_id}", response_model=TaskInfo)
    def task_status(task_id: str) -> TaskInfo:
        assert ctx.tasks is not None
        return ctx.tasks.get(task_id).info()

    @router.get("/tasks/{task_id}/events")
    def task_events(
        task_id: str,
        request: Request,
        from_sequence: int = Query(0),
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        assert ctx.tasks is not None
        task = ctx.tasks.get(task_id)
        start = resume_sequence(last_event_id, from_sequence)
        return StreamingResponse(
            sse_log_stream(
                task.log, start, terminal_events=TASK_TERMINAL_EVENTS
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    return router


__all__ = ["TASK_TERMINAL_EVENTS", "StudioTask", "TaskRegistry", "build_router"]
