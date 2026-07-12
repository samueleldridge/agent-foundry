"""Endpoint generation — the docs/70 catalogue, introspected per project.

No hand-written per-project routes: ``build_routers`` takes the
CompiledProject-derived input/output models and attaches the full
catalogue with those models as the request/response annotations, so
``/openapi.json`` IS the SystemSpec contract (docs/70 load-bearing
property 2). Auth applies to every route except ``/health`` (and
FastAPI's own ``/openapi.json`` + ``/docs``, which are app-level).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from foundry.api.auth import AuthBackend, AuthContext
from foundry.api.batch import BatchRequest, execute_batch
from foundry.api.errors import status_for_error_class
from foundry.api.runs import LiveRun, RunManager
from foundry.api.schemas import (
    ConfigSnapshot,
    DependencyHealth,
    Health,
    PendingApproval,
    RunAccepted,
    RunStatusResponse,
)
from foundry.api.streaming import handle_websocket, sse_run_stream
from foundry.core import (
    ApprovalResponse,
    CancelRun,
    InboundMessage,
    PauseRun,
    ResumeRun,
)
from foundry.core.errors import OrchestrationError

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive"}

_SOFT_FAIL_ERRORS = frozenset(
    # docs/70 § Failure modes: these run outcomes are 200 + status=failed
    # (the run executed; ITS budget/contract failed), not transport errors.
    {"CostBudgetExceeded", "ToolOutputValidationError", "OutputValidationError"}
)


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def _run_headers(manager: RunManager, run_id: str) -> dict[str, str]:
    return {"X-Foundry-Run-Id": run_id}


def _unavailable(manager: RunManager) -> JSONResponse:
    reason = (
        "worker is draining (shutdown in progress)"
        if manager.worker_state.draining
        else "worker at max concurrent runs"
    )
    return JSONResponse(
        status_code=503,
        content={
            "error_class": "ServiceUnavailable",
            "message": reason,
            "context": {
                "max_concurrent_runs": manager.max_concurrent_runs,
                "draining": manager.worker_state.draining,
            },
        },
        headers={"Retry-After": "5"},
    )


async def _await_decision(live: LiveRun) -> None:
    """Wait until the run reaches a terminal state or pauses on an
    approval (the non-streaming caller's two possible answers)."""
    while True:
        await live.attention.wait()
        live.attention.clear()
        if live.status == "approval_pending" or not live.active:
            return


def _pending_payload(live: LiveRun) -> dict[str, Any]:
    return RunAccepted(
        run_id=str(live.run_id),
        status="approval_pending",
        pending_approval=(
            PendingApproval.model_validate(live.pending_approval)
            if live.pending_approval
            else None
        ),
        resume_url=f"/runs/{live.run_id}/resume",
    ).model_dump(mode="json")


def _completed_or_error(
    manager: RunManager, live: LiveRun, output_model: type[BaseModel]
) -> JSONResponse:
    headers = _run_headers(manager, str(live.run_id))
    if live.status == "approval_pending":
        # docs/70: ApprovalRequired on a non-interactive surface → 409
        # with the run_id + status hint; resolve via /resume.
        return JSONResponse(
            status_code=409, content=_pending_payload(live), headers=headers
        )
    if live.status in ("cancelled", "paused"):
        return JSONResponse(
            status_code=499,
            content={
                "run_id": str(live.run_id),
                "status": live.status,
                "reason": live.cancel_reason,
            },
            headers=headers,
        )
    if live.status == "failed":
        error = live.error or {}
        error_class = str(error.get("error_class", ""))
        if error_class in _SOFT_FAIL_ERRORS:
            return JSONResponse(
                status_code=200,
                content={
                    "run_id": str(live.run_id),
                    "status": "failed",
                    "error": error,
                },
                headers=headers,
            )
        return JSONResponse(
            status_code=status_for_error_class(error_class),
            content=error,
            headers=headers,
        )
    validated = output_model.model_validate(_jsonable(live.output))
    return JSONResponse(
        status_code=200,
        content=validated.model_dump(mode="json"),
        headers=headers,
    )


def build_routers(
    manager: RunManager,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    auth_backend: AuthBackend,
    framework_version: str,
) -> tuple[APIRouter, APIRouter]:
    """(protected_router, open_router) — the docs/70 endpoint catalogue."""

    async def _auth(request: Request) -> AuthContext:
        return await auth_backend.authenticate(request)

    protected = APIRouter(dependencies=[Depends(_auth)])
    open_router = APIRouter()
    compiled = manager.compiled
    project = manager.project

    # --- POST /run (docs/70 § non-streaming run) --------------------------------

    async def run_endpoint(body: Any) -> Any:
        if not manager.can_accept():
            return _unavailable(manager)
        live = manager.start_run(body.model_dump(mode="json"))
        await _await_decision(live)
        return _completed_or_error(manager, live, output_model)

    run_endpoint.__annotations__ = {"body": input_model, "return": Any}
    protected.add_api_route(
        "/run",
        run_endpoint,
        methods=["POST"],
        response_model=output_model,
        summary=f"Run {project} once and return its typed output.",
        responses={
            409: {"model": RunAccepted, "description": "approval pending"},
            503: {"description": "draining or at capacity"},
        },
    )

    # --- POST /stream (docs/70 § SSE streaming run) ---------------------------------

    async def stream_endpoint(body: Any) -> Any:
        if not manager.can_accept():
            return _unavailable(manager)
        live = manager.start_run(body.model_dump(mode="json"))
        return StreamingResponse(
            sse_run_stream(
                manager,
                str(live.run_id),
                live.base_sequence,
                cancel_on_disconnect=True,
            ),
            media_type="text/event-stream",
            headers={
                **_run_headers(manager, str(live.run_id)),
                **_SSE_HEADERS,
            },
        )

    stream_endpoint.__annotations__ = {"body": input_model, "return": Any}
    protected.add_api_route(
        "/stream",
        stream_endpoint,
        methods=["POST"],
        summary=f"Run {project} streaming RunEvents over SSE.",
    )

    # --- POST /batch (docs/85) ---------------------------------------------------

    async def batch_endpoint(body: BatchRequest) -> Any:
        if not manager.can_accept():
            return _unavailable(manager)
        if len(body.items) > manager.max_batch_items:
            # Batch-size cap (Phase 9 pre-work): one request must not be
            # able to enqueue unbounded work. Env: FOUNDRY_MAX_BATCH_ITEMS.
            return JSONResponse(
                status_code=413,
                content={
                    "error_class": "RequestTooLarge",
                    "message": (
                        f"batch has {len(body.items)} items; this worker "
                        f"caps batches at {manager.max_batch_items} "
                        "(FOUNDRY_MAX_BATCH_ITEMS)"
                    ),
                    "context": {
                        "items": len(body.items),
                        "max_batch_items": manager.max_batch_items,
                    },
                },
            )
        for item in body.items:
            try:
                input_model.model_validate(item.input)
            except ValidationError as exc:
                return JSONResponse(
                    status_code=400,
                    content={
                        "error_class": "ConfigValidationError",
                        "message": (
                            f"batch item {item.item_id!r} does not match "
                            "the project input schema: "
                            f"{exc.errors()[0]['msg']}"
                        ),
                        "context": {
                            "item_id": item.item_id,
                            "errors": [
                                {
                                    "field": ".".join(
                                        str(p) for p in e["loc"]
                                    ),
                                    "reason": e["msg"],
                                }
                                for e in exc.errors()
                            ],
                        },
                    },
                )
        batch_id = body.resolved_batch_id()
        body = body.model_copy(update={"batch_id": batch_id})
        return StreamingResponse(
            execute_batch(manager, body),
            media_type="text/event-stream",
            headers={"X-Foundry-Batch-Id": batch_id, **_SSE_HEADERS},
        )

    protected.add_api_route(
        "/batch",
        batch_endpoint,
        methods=["POST"],
        summary="Run a batch of inputs; per-item RunEvents over one SSE "
        "stream tagged batch_id/item_id.",
    )

    # --- WS /ws --------------------------------------------------------------------

    async def ws_endpoint(websocket: WebSocket) -> None:
        # WebSockets can't use the HTTP Request dependency; authenticate
        # explicitly against the same backend (headers duck-type). A
        # rejected handshake closes with 1008 (policy violation).
        try:
            await auth_backend.authenticate(websocket)  # type: ignore[arg-type]
        except HTTPException:
            await websocket.close(code=1008, reason="authentication failed")
            return
        await handle_websocket(websocket, manager, input_model)

    open_router.add_api_websocket_route("/ws", ws_endpoint)

    # --- GET /runs/{run_id} -----------------------------------------------------

    async def run_status_endpoint(run_id: str) -> Any:
        status = manager.run_status(run_id)
        if status is None:
            return JSONResponse(
                status_code=404, content={"error": "run_id not found"}
            )
        return status

    protected.add_api_route(
        "/runs/{run_id}",
        run_status_endpoint,
        methods=["GET"],
        response_model=RunStatusResponse,
        summary="Run status (read-only).",
    )

    # --- GET /runs/{run_id}/events (SSE replay + live catch-up) ---------------------

    async def run_events_endpoint(
        run_id: str, request: Request, from_sequence: int = -1
    ) -> Any:
        last_event_id = request.headers.get("Last-Event-ID")
        after = from_sequence
        if last_event_id is not None:
            try:
                after = max(after, int(last_event_id))
            except ValueError:
                pass
        if not manager.owns_artifact(run_id):
            # Unknown run OR another project's artifact under a shared
            # FOUNDRY_HOME — both read as not-found (no cross-project
            # event leakage; Phase 9 pre-work).
            return JSONResponse(
                status_code=404, content={"error": "run_id not found"}
            )
        return StreamingResponse(
            sse_run_stream(manager, run_id, after + 1),
            media_type="text/event-stream",
            headers={**_run_headers(manager, run_id), **_SSE_HEADERS},
        )

    protected.add_api_route(
        "/runs/{run_id}/events",
        run_events_endpoint,
        methods=["GET"],
        summary="Replay persisted RunEvents from a sequence (Last-Event-ID"
        " reconnect); continues live when the run is still active.",
    )

    # --- POST /runs/{run_id}/resume ------------------------------------------------

    async def resume_endpoint(run_id: str, body: InboundMessage) -> Any:
        if isinstance(body, ApprovalResponse):
            live = manager.deliver_approval(
                run_id,
                {
                    "approval_id": body.approval_id,
                    "decision": body.decision,
                    "reason": body.reason,
                },
            )
            await _await_decision(live)
            return _completed_or_error(manager, live, output_model)
        if isinstance(body, ResumeRun):
            existing = manager.get(run_id)
            if existing is not None and existing.active:
                return JSONResponse(
                    status_code=409,
                    content={
                        "run_id": run_id,
                        "status": existing.status,
                        "error": "run is still active on this worker",
                    },
                )
            metadata = manager.read_artifact_metadata(run_id)
            if metadata is None or metadata.get("project") != manager.project:
                # Another project's run under a shared FOUNDRY_HOME is not
                # resumable here — and reads as not-found (no existence
                # leakage across projects; mirrors deliver_approval).
                return JSONResponse(
                    status_code=404, content={"error": "run_id not found"}
                )
            if metadata.get("status") not in (
                "cancelled",
                "paused",
                "failed",
                "approval_pending",
            ):
                return JSONResponse(
                    status_code=409,
                    content={
                        "run_id": run_id,
                        "status": metadata.get("status"),
                        "error": "run is not resumable",
                    },
                )
            live = manager.resume_run(run_id)
            await _await_decision(live)
            return _completed_or_error(manager, live, output_model)
        if isinstance(body, CancelRun):
            if not manager.cancel(run_id, body.reason or "user_abort"):
                return JSONResponse(
                    status_code=404,
                    content={"error": "run_id not active on this worker"},
                )
            return {"run_id": run_id, "status": "cancelling"}
        if isinstance(body, PauseRun):
            if not manager.cancel(run_id, "pause"):
                return JSONResponse(
                    status_code=404,
                    content={"error": "run_id not active on this worker"},
                )
            return {"run_id": run_id, "status": "pausing"}
        raise OrchestrationError(
            f"inbound kind {body.kind!r} is not supported on POST /resume — "
            "inject_input is a WebSocket-surface message in v1 (docs/70; "
            "chat-style continuations are v1.1+)",
            context={"run_id": run_id, "kind": body.kind},
        )

    protected.add_api_route(
        "/runs/{run_id}/resume",
        resume_endpoint,
        methods=["POST"],
        summary="Resume a paused/interrupted run: approval_response, "
        "resume, cancel, pause.",
    )

    # --- GET /config -----------------------------------------------------------------

    config_snapshot = ConfigSnapshot(
        project=project,
        system_version=compiled.system_version,
        pin_set_hash=compiled.pin_set_hash,
        framework_version=framework_version,
        agents=sorted(compiled.agent_map()),
        functions=sorted(compiled.functions),
        flow_pattern=compiled.flow_plan().pattern,
        tools_pinned={
            name: f"{binding.ref}@{binding.version}"
            for name, binding in compiled.project.system.tools.items()
        },
        connections={
            name: {
                "ref": binding.ref,
                "version": binding.version,
            }
            for name, binding in compiled.project.system.connections.items()
        },
        guardrails=compiled.project.system.guardrails.model_dump(mode="json"),
        compiled_at=datetime.now(UTC),
    )

    async def config_endpoint() -> ConfigSnapshot:
        return config_snapshot

    protected.add_api_route(
        "/config",
        config_endpoint,
        methods=["GET"],
        response_model=ConfigSnapshot,
        summary="Redacted compiled-system snapshot.",
    )

    # --- GET /health (open; docs/70 § liveness vs readiness) --------------------------

    async def health_endpoint(deep: bool = False) -> Any:
        worker = manager.worker_state
        if not deep:
            return Health(
                status="alive",
                uptime_s=worker.uptime_s,
                worker_id=worker.worker_id,
                project=project,
            )
        checkpointer = _checkpointer_health(manager)
        payload = Health(
            status="draining" if worker.draining else "ready",
            uptime_s=worker.uptime_s,
            worker_id=worker.worker_id,
            project=project,
            checkpointer=checkpointer,
            rate_limiter=DependencyHealth(ok=True),
        )
        degraded = worker.draining or not checkpointer.ok
        if degraded and payload.status == "ready":
            payload = payload.model_copy(update={"status": "degraded"})
        return JSONResponse(
            status_code=503 if degraded else 200,
            content=payload.model_dump(mode="json"),
        )

    open_router.add_api_route(
        "/health",
        health_endpoint,
        methods=["GET"],
        response_model=Health,
        summary="Liveness (cheap); ?deep=true adds dependency checks "
        "(readiness) and reports draining as 503.",
    )

    return protected, open_router


def _checkpointer_health(manager: RunManager) -> DependencyHealth:
    if manager.checkpoint in ("memory", "none"):
        return DependencyHealth(ok=True)
    from foundry.runtime.checkpointers import default_checkpoint_db

    path = manager.checkpoint_db or default_checkpoint_db(manager.project)
    started = time.monotonic()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        probe = path.parent / f".probe-{manager.worker_state.worker_id.replace(':', '-')}"
        probe.write_text("ok")
        probe.unlink()
    except OSError as exc:
        return DependencyHealth(ok=False, error=str(exc))
    return DependencyHealth(
        ok=True, latency_ms=int((time.monotonic() - started) * 1000)
    )


__all__ = ["build_routers"]
