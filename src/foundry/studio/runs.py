"""Run history + RunArtifact readers + approvals inbox + resume (docs/72 §
Runs + artifacts + approvals).

History merges the SQLite mirror with the artifact store's metadata
(approval-pending pauses live in metadata before the mirror sees a
terminal event). Every payload passes the redactor before serialisation.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

from foundry.api.streaming import sse_run_stream
from foundry.core.errors import ConfigLoadError
from foundry.observability.events import get_store
from foundry.observability.store import parse_since
from foundry.storage.paths import run_dir, runs_root
from foundry.studio.context import StudioContext
from foundry.studio.events import emit_studio_event, resume_sequence
from foundry.studio.schemas import (
    ApprovalItem,
    ResumeRequest,
    ResumeResponse,
    RunArtifactView,
    RunListItem,
)
from foundry.studio.security import redacted


def _read_json(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _read_jsonl(path: Path, limit: int = 500) -> list[dict[str, Any]]:
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
        if len(rows) >= limit:
            break
    return rows


def run_metadata(run_id: str) -> dict[str, Any]:
    metadata = _read_json(run_dir(run_id) / "metadata.json")
    if metadata is None:
        raise ConfigLoadError(
            f"run {run_id!r} not found",
            context={"run_id": run_id, "not_found": True},
        )
    return metadata


def pending_approvals(project: str | None = None) -> list[ApprovalItem]:
    """The approvals inbox: approval-pending runs across local artifacts
    (the `foundry approvals list` scan, structured)."""
    root = runs_root()
    items: list[ApprovalItem] = []
    if not root.is_dir():
        return items
    for directory in sorted(root.iterdir()):
        metadata = _read_json(directory / "metadata.json")
        if not metadata or metadata.get("status") != "approval_pending":
            continue
        if project and metadata.get("project") != project:
            continue
        pending = metadata.get("pending_approval") or {}
        items.append(
            ApprovalItem(
                run_id=directory.name,
                project=str(metadata.get("project", "")),
                approval_id=str(pending.get("approval_id", "")),
                prompt=str(pending.get("prompt", "")),
                agent_name=str(pending.get("agent_name", "")),
                context=redacted(dict(pending.get("context") or {})),
            )
        )
    return items


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.get("/runs", response_model=list[RunListItem])
    def list_runs(
        project: str | None = Query(None),
        since: str | None = Query(None),
        status: str | None = Query(None),
    ) -> list[RunListItem]:
        rows = get_store().recent_runs(
            project=project,
            since=parse_since(since) if since else None,
            status=status,
        )
        items: list[RunListItem] = []
        for row in rows:
            run_id = str(row.get("run_id", ""))
            metadata = _read_json(run_dir(run_id) / "metadata.json") or {}
            items.append(
                RunListItem(
                    run_id=run_id,
                    project=str(row.get("project", "")),
                    # Metadata carries pause states the mirror reads as
                    # in_progress (approval_pending / paused).
                    status=str(metadata.get("status") or row.get("status", "")),
                    started_at=row.get("started_at"),
                    completed_at=row.get("completed_at"),
                    total_cost_usd=row.get("total_cost_usd"),
                    total_tokens=(
                        int(row.get("total_input_tokens") or 0)
                        + int(row.get("total_output_tokens") or 0)
                    ),
                    error_class=row.get("error_class"),
                )
            )
        return items

    @router.get("/runs/{run_id}")
    def run_status(run_id: str) -> dict[str, Any]:
        metadata = run_metadata(run_id)
        return {
            "run_id": run_id,
            "project": metadata.get("project", ""),
            "status": metadata.get("status", "unknown"),
            "pending_approval": redacted(
                metadata.get("pending_approval") or None
            ),
            "error": redacted(metadata.get("error") or None),
            "events_url": f"/api/runs/{run_id}/events",
        }

    @router.get("/runs/{run_id}/events")
    async def run_events(
        run_id: str,
        from_sequence: int = Query(0),
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        metadata = run_metadata(run_id)
        project = str(metadata.get("project", ""))
        assert ctx.chat is not None
        # Live handover when the run belongs to a pooled project manager;
        # persisted-artifact replay otherwise (docs/70 § Last-Event-ID).
        manager = ctx.chat.manager_for(project)
        start = resume_sequence(last_event_id, from_sequence)
        return StreamingResponse(
            sse_run_stream(manager, run_id, start),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @router.get("/runs/{run_id}/artifact", response_model=RunArtifactView)
    def run_artifact(run_id: str) -> RunArtifactView:
        metadata = run_metadata(run_id)
        directory = run_dir(run_id)
        events = _read_jsonl(directory / "events.jsonl", limit=100_000)
        return RunArtifactView(
            run_id=run_id,
            metadata=redacted(metadata),
            # Both files persist under a wrapper key (docs/81) — unwrap so
            # consumers get the actual payloads, not the envelopes.
            inputs=redacted(
                (_read_json(directory / "inputs.json") or {}).get("inputs")
            ),
            outputs=redacted(
                (_read_json(directory / "outputs.json") or {}).get("output")
            ),
            state_transitions=redacted(
                _read_jsonl(directory / "state_transitions.jsonl")
            ),
            llm_calls=redacted(_read_jsonl(directory / "llm_calls.jsonl")),
            tool_calls=redacted(_read_jsonl(directory / "tool_calls.jsonl")),
            event_count=len(events),
        )

    @router.get("/approvals", response_model=list[ApprovalItem])
    def approvals(project: str | None = Query(None)) -> list[ApprovalItem]:
        return pending_approvals(project)

    @router.post("/runs/{run_id}/resume", response_model=ResumeResponse)
    async def resume_run(
        run_id: str, body: ResumeRequest, request: Request
    ) -> ResumeResponse:
        metadata = run_metadata(run_id)
        project = str(metadata.get("project", ""))
        assert ctx.chat is not None
        manager = ctx.chat.manager_for(project)
        live = manager.deliver_approval(
            run_id,
            {
                "approval_id": body.approval_id,
                "decision": body.decision,
                "reason": body.reason,
            },
        )
        emit_studio_event(
            "studio.approval_resolved",
            project=project,
            studio_request_id=getattr(
                request.state, "studio_request_id", ""
            ),
            run_id=run_id,
            approval_id=body.approval_id,
            decision=body.decision,
        )
        return ResumeResponse(
            run_id=str(live.run_id),
            status=live.status,
            events_url=f"/api/runs/{live.run_id}/events",
        )

    return router


__all__ = ["build_router", "pending_approvals", "run_metadata"]
