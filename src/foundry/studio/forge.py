"""Forge lifecycle: launch (supervised background task), live SSE
trajectory, list / show, cancel (docs/72 § Forge lifecycle + streaming).

One forge run per project at a time — a concurrent launch for the same
project is a 409 carrying the active ``forge_run_id``. The session's
progress events (iterations, scores, per-iteration commit shas, meta-tool
calls, sandbox violations, termination reason) stream over SSE with
Last-Event-ID resume; cancel finalises the trajectory artifact as
``user_cancelled``.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import anyio
from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from foundry.core.errors import (
    ConfigLoadError,
    ConfigValidationError,
    FoundryError,
)
from foundry.storage.paths import run_dir, runs_root
from foundry.studio.context import StudioContext
from foundry.studio.events import (
    EventLog,
    emit_studio_event,
    resume_sequence,
    sse_log_stream,
)
from foundry.studio.schemas import (
    ForgeLaunchRequest,
    ForgeLaunchResponse,
    ForgeRunInfo,
)

FORGE_TERMINAL_EVENTS = frozenset({"forge.terminated", "forge.failed"})


@dataclass
class ForgeTask:
    project: str
    threshold: float
    forge_run_id: str = ""
    status: str = "running"
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    log: EventLog = field(default_factory=EventLog)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    error: dict[str, Any] | None = None
    result: Any = None
    scope: anyio.CancelScope | None = None
    cancel_requested: bool = False


class ForgeSupervisor:
    """project → active ForgeTask (+ completed history for this process)."""

    def __init__(self, ctx: StudioContext) -> None:
        self._ctx = ctx
        self._active: dict[str, ForgeTask] = {}
        self._by_id: dict[str, ForgeTask] = {}

    def active_count(self) -> int:
        return sum(
            1 for task in self._active.values() if task.status == "running"
        )

    def get(self, forge_run_id: str) -> ForgeTask | None:
        return self._by_id.get(forge_run_id)

    def active_for(self, project: str) -> ForgeTask | None:
        task = self._active.get(project)
        if task is not None and task.status == "running":
            return task
        return None

    def running_tasks(self) -> dict[str, ForgeTask]:
        return {
            forge_run_id: task
            for forge_run_id, task in self._by_id.items()
            if task.status == "running"
        }

    async def launch(self, body: ForgeLaunchRequest) -> ForgeTask:
        from foundry.configurator import (
            ForgeGuardrails,
            ForgeSession,
            MetaAgent,
        )
        from foundry.providers import ModelBinding, ModelSettings

        project_dir = self._resolve_project_dir(body.project)
        cost_cap: Decimal | None = None
        if body.max_cost_usd is not None:
            try:
                cost_cap = Decimal(body.max_cost_usd)
            except InvalidOperation as exc:
                raise ConfigValidationError(
                    f"max_cost_usd must be a decimal amount, got "
                    f"{body.max_cost_usd!r}",
                    context={"max_cost_usd": body.max_cost_usd},
                    cause=exc,
                ) from exc
        binding: ModelBinding | None = None
        if body.model is not None:
            if "/" not in body.model:
                raise ConfigValidationError(
                    f"model must be '<provider>/<model>', got {body.model!r}",
                    context={"model": body.model},
                )
            provider, model_name = body.model.split("/", 1)
            binding = ModelBinding(
                provider=provider,
                model=model_name,
                settings=ModelSettings(temperature=0.1, max_tokens=4096),
            )
        eval_path = (self._ctx.repo_root / body.eval_path).resolve()

        task = ForgeTask(project=body.project, threshold=body.threshold)

        def sink(event: BaseModel) -> None:
            data: dict[str, Any] = json.loads(event.model_dump_json())
            task.log.append(data)
            kind = str(data.get("event", ""))
            if kind == "forge.started":
                task.forge_run_id = str(data.get("forge_run_id", ""))
                self._by_id[task.forge_run_id] = task
                task.started.set()

        meta_agent = MetaAgent(
            project_dir.name,
            projects_root=project_dir.parent,
            model_binding=binding,
            guardrails=ForgeGuardrails(
                max_iter=body.max_iter,
                max_cost_usd=cost_cap,
                no_improvement_after=body.no_improvement_after,
            ),
            transport=self._ctx.transport,
        )
        session = ForgeSession(
            meta_agent=meta_agent,
            description=body.description,
            eval_spec_path=eval_path,
            threshold=body.threshold,
            event_sink=sink,
        )
        self._active[body.project] = task
        self._ctx.spawn(self._drive, task, session)
        # The launch response needs the forge_run_id, which the session
        # mints in run() and announces via forge.started. Pre-flight
        # failures (dirty tree, missing eval) surface before any event.
        with anyio.move_on_after(30):
            await task.started.wait()
        if task.error is not None:
            raise FoundryError(
                str(task.error.get("message", "forge launch failed")),
                context=dict(task.error.get("context") or {}),
            )
        if not task.started.is_set():
            raise FoundryError(
                "forge session did not start within 30s",
                context={"project": body.project},
            )
        return task

    def _resolve_project_dir(self, project: str) -> Path:
        """Bootstrap-able projects (foundry project new) have no
        system.yaml yet — resolve by directory, like `foundry forge`."""
        candidate = self._ctx.projects_root / project
        if candidate.is_dir():
            return candidate.resolve()
        raise ConfigLoadError(
            f"project {project!r} not found; create it with "
            "`foundry project new <name>`",
            context={"project": project, "not_found": True},
        )

    async def _drive(self, task: ForgeTask, session: Any) -> None:
        try:
            with anyio.CancelScope() as scope:
                task.scope = scope
                task.result = await session.run()
            if scope.cancelled_caught:
                self._finish_cancelled(task)
                return
            task.status = "completed"
        except FoundryError as exc:
            task.status = "failed"
            task.error = exc.to_dict()
            if not task.started.is_set():
                task.started.set()
            task.log.append({"event": "forge.failed", "error": task.error})
        except BaseException:
            # Lifespan teardown: leave artifacts as-is; resumable.
            self._finish_cancelled(task)
            raise
        finally:
            if task.status != "running":
                task.log.close()
            self._active.pop(task.project, None)

    def _finish_cancelled(self, task: ForgeTask) -> None:
        """Finalise a cancelled forge: the session never got to write its
        terminal artifacts, so the supervisor writes the cancelled
        ForgeResult over whatever trajectory reached disk."""
        task.status = "cancelled"
        if task.forge_run_id:
            self._write_cancelled_meta(task)
        task.log.append(
            {
                "event": "forge.terminated",
                "forge_run_id": task.forge_run_id,
                "reason": "user_cancelled",
                "final_score": None,
                "iterations": _iteration_count(task.forge_run_id),
                "total_cost_usd": None,
            }
        )
        task.log.close()

    def _write_cancelled_meta(self, task: ForgeTask) -> None:
        from foundry.configurator.session import ForgeResult

        artifact_dir = run_dir(task.forge_run_id)
        if (artifact_dir / "meta.json").is_file():
            return  # the session finalised before the cancel landed
        artifact_dir.mkdir(parents=True, exist_ok=True)
        trajectory = _read_trajectory(artifact_dir)
        now = datetime.now(UTC)
        result = ForgeResult(
            forge_run_id=task.forge_run_id,
            project=task.project,
            started_at=task.started_at,
            completed_at=now,
            duration_s=(now - task.started_at).total_seconds(),
            final_score=_last_score(trajectory) or 0.0,
            best_score=_best_score(trajectory),
            threshold=task.threshold,
            threshold_met=False,
            iterations=sum(
                1 for record in trajectory if record.get("kind") == "iterate"
            ),
            bootstrap=any(
                record.get("kind") == "bootstrap" for record in trajectory
            ),
            termination_reason="user_cancelled",
            termination_detail="cancelled from the studio",
            trajectory=[],
            total_cost_usd=None,
            total_tokens=0,
            meta_agent_version="",
            artifact_dir=str(artifact_dir),
        )
        (artifact_dir / "meta.json").write_text(
            result.model_dump_json(indent=2, exclude={"trajectory"}) + "\n"
        )

    def cancel(self, forge_run_id: str) -> bool:
        task = self._by_id.get(forge_run_id)
        if task is None or task.status != "running":
            return False
        task.cancel_requested = True
        if task.scope is not None:
            task.scope.cancel()
        return True


def _read_trajectory(artifact_dir: Path) -> list[dict[str, Any]]:
    path = artifact_dir / "trajectory.jsonl"
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict):
            rows.append(data)
    return rows


def _last_score(trajectory: list[dict[str, Any]]) -> float | None:
    for record in reversed(trajectory):
        score = record.get("eval_score_after")
        if isinstance(score, int | float):
            return float(score)
    return None


def _best_score(trajectory: list[dict[str, Any]]) -> float:
    scores = [
        float(record["eval_score_after"])
        for record in trajectory
        if isinstance(record.get("eval_score_after"), int | float)
    ]
    return max(scores, default=0.0)


def _iteration_count(forge_run_id: str) -> int:
    if not forge_run_id:
        return 0
    return sum(
        1
        for record in _read_trajectory(run_dir(forge_run_id))
        if record.get("kind") == "iterate"
    )


def _info_from_artifacts(directory: Path) -> ForgeRunInfo | None:
    meta = directory / "meta.json"
    if not meta.is_file():
        return None
    try:
        data = json.loads(meta.read_text())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "forge_run_id" not in data:
        return None
    reason = data.get("termination_reason")
    return ForgeRunInfo(
        forge_run_id=str(data.get("forge_run_id", directory.name)),
        project=str(data.get("project", "")),
        status=(
            "cancelled" if reason == "user_cancelled" else "completed"
        ),
        threshold=data.get("threshold"),
        final_score=data.get("final_score"),
        best_score=data.get("best_score"),
        iterations=int(data.get("iterations") or 0),
        termination_reason=reason,
        termination_detail=str(data.get("termination_detail") or ""),
        started_at=data.get("started_at"),
        completed_at=data.get("completed_at"),
        total_cost_usd=(
            str(data["total_cost_usd"])
            if data.get("total_cost_usd") is not None
            else None
        ),
        trajectory=_read_trajectory(directory),
    )


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.post("/forge", status_code=202)
    async def launch_forge(
        body: ForgeLaunchRequest, request: Request
    ) -> object:
        assert ctx.forge is not None
        active = ctx.forge.active_for(body.project)
        if active is not None:
            return JSONResponse(
                status_code=409,
                content={
                    "error_class": "ForgeAlreadyRunning",
                    "message": (
                        f"a forge run is already active for project "
                        f"{body.project!r} — watch it instead"
                    ),
                    "context": {
                        "project": body.project,
                        "forge_run_id": active.forge_run_id,
                    },
                },
            )
        task = await ctx.forge.launch(body)
        emit_studio_event(
            "studio.forge_launched",
            project=body.project,
            studio_request_id=getattr(
                request.state, "studio_request_id", ""
            ),
            forge_run_id=task.forge_run_id,
        )
        return ForgeLaunchResponse(
            forge_run_id=task.forge_run_id,
            project=body.project,
            events_url=f"/api/forge/{task.forge_run_id}/events",
        )

    @router.get("/forge", response_model=list[ForgeRunInfo])
    def list_forge_runs(
        project: str | None = Query(None),
    ) -> list[ForgeRunInfo]:
        assert ctx.forge is not None
        rows: dict[str, ForgeRunInfo] = {}
        root = runs_root()
        if root.is_dir():
            for directory in sorted(root.iterdir()):
                info = _info_from_artifacts(directory)
                if info is not None:
                    rows[info.forge_run_id] = info
        # Live tasks overlay the artifact view.
        for forge_run_id, task in ctx.forge.running_tasks().items():
            rows[forge_run_id] = ForgeRunInfo(
                forge_run_id=forge_run_id,
                project=task.project,
                status="running",
                threshold=task.threshold,
                started_at=task.started_at.isoformat(),
                trajectory=_read_trajectory(run_dir(forge_run_id)),
            )
        return [
            info
            for info in rows.values()
            if project is None or info.project == project
        ]

    @router.get("/forge/{forge_run_id}", response_model=ForgeRunInfo)
    def show_forge_run(forge_run_id: str) -> ForgeRunInfo:
        assert ctx.forge is not None
        task = ctx.forge.get(forge_run_id)
        if task is not None and task.status == "running":
            return ForgeRunInfo(
                forge_run_id=forge_run_id,
                project=task.project,
                status="running",
                threshold=task.threshold,
                started_at=task.started_at.isoformat(),
                trajectory=_read_trajectory(run_dir(forge_run_id)),
            )
        info = _info_from_artifacts(run_dir(forge_run_id))
        if info is None:
            raise ConfigLoadError(
                f"forge run {forge_run_id!r} not found",
                context={"forge_run_id": forge_run_id, "not_found": True},
            )
        return info

    @router.get("/forge/{forge_run_id}/events")
    def forge_events(
        forge_run_id: str,
        from_sequence: int = Query(0),
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        assert ctx.forge is not None
        task = ctx.forge.get(forge_run_id)
        if task is None:
            raise ConfigLoadError(
                f"forge run {forge_run_id!r} has no live event stream in "
                "this studio process (historical trajectories: "
                f"GET /api/forge/{forge_run_id})",
                context={"forge_run_id": forge_run_id, "not_found": True},
            )
        start = resume_sequence(last_event_id, from_sequence)
        return StreamingResponse(
            sse_log_stream(
                task.log, start, terminal_events=FORGE_TERMINAL_EVENTS
            ),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @router.post("/forge/{forge_run_id}/cancel")
    async def cancel_forge(forge_run_id: str) -> dict[str, Any]:
        # async ON PURPOSE: CancelScope.cancel() must run on the event
        # loop (a threadpool'd sync handler would cancel from a foreign
        # thread — silently ineffective).
        assert ctx.forge is not None
        if not ctx.forge.cancel(forge_run_id):
            raise ConfigLoadError(
                f"forge run {forge_run_id!r} is not active",
                context={"forge_run_id": forge_run_id, "not_found": True},
            )
        return {"forge_run_id": forge_run_id, "cancelling": True}

    return router


__all__ = ["ForgeSupervisor", "ForgeTask", "build_router"]
