"""Observability queries over the local SQLite mirror (docs/72 §
Observability). All queries go through ``foundry.observability.store`` —
never raw SQL in the studio layer."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Query

from foundry.observability.events import get_store
from foundry.observability.store import parse_since
from foundry.studio.context import StudioContext
from foundry.studio.schemas import ObsRows


def _since(value: str | None) -> datetime | None:
    return parse_since(value) if value else None


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.get("/obs/cost", response_model=ObsRows)
    def obs_cost(
        project: str | None = Query(None),
        since: str | None = Query(None),
        by: str = Query("model"),
    ) -> ObsRows:
        return ObsRows(
            rows=get_store().cost_breakdown(
                project=project, since=_since(since), by=by
            )
        )

    @router.get("/obs/latency", response_model=ObsRows)
    def obs_latency(
        model: str | None = Query(None),
        project: str | None = Query(None),
        since: str | None = Query(None),
    ) -> ObsRows:
        return ObsRows(
            rows=get_store().latency_percentiles(
                model=model, project=project, since=_since(since)
            )
        )

    @router.get("/obs/tool-failures", response_model=ObsRows)
    def obs_tool_failures(
        tool: str | None = Query(None),
        project: str | None = Query(None),
        since: str | None = Query(None),
    ) -> ObsRows:
        return ObsRows(
            rows=get_store().tool_failures(
                tool_ref=tool, project=project, since=_since(since)
            )
        )

    @router.get("/obs/eval-trend", response_model=ObsRows)
    def obs_eval_trend(
        project: str | None = Query(None),
        since: str | None = Query(None),
    ) -> ObsRows:
        return ObsRows(
            rows=get_store().eval_rows(project=project, since=_since(since))
        )

    @router.get("/obs/runs", response_model=ObsRows)
    def obs_runs(
        project: str | None = Query(None),
        since: str | None = Query(None),
        status: str | None = Query(None),
    ) -> ObsRows:
        return ObsRows(
            rows=get_store().recent_runs(
                project=project, since=_since(since), status=status
            )
        )

    return router


__all__ = ["build_router"]
