"""Eval launch (background) + results + compare (docs/72 § Evals).

Launches run in the studio lifespan task group via the task registry;
progress + the terminal result stream over ``/api/tasks/{id}/events``.
Results delegate to the eval harness's own artifact readers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Query

from foundry.config.loader import load_eval_spec
from foundry.config.refs import FoundryRoots
from foundry.core.errors import ConfigLoadError, ConfigValidationError
from foundry.eval.compare import compare_project_pin_sets, compare_tool_versions
from foundry.eval.harness import (
    AgentEvalTarget,
    ProjectEvalTarget,
    load_eval_result,
    load_tool_target,
    run_eval,
)
from foundry.observability.events import get_store
from foundry.studio.context import StudioContext
from foundry.studio.schemas import (
    EvalCompareRequest,
    EvalLaunchRequest,
    EvalRunRow,
    TaskLaunched,
)
from foundry.studio.tasks import StudioTask


def _resolve_eval_path(
    ctx: StudioContext, project_dir: Path, eval_set: str | None
) -> Path:
    if eval_set:
        for candidate in (project_dir / eval_set, ctx.repo_root / eval_set):
            if candidate.is_file():
                return candidate.resolve()
        raise ConfigLoadError(
            f"eval set {eval_set!r} not found (checked project-relative "
            "and repo-relative)",
            context={"eval_set": eval_set, "not_found": True},
        )
    evals_dir = project_dir / "evals"
    candidates = sorted(evals_dir.glob("*.yaml")) if evals_dir.is_dir() else []
    if len(candidates) != 1:
        raise ConfigValidationError(
            "eval_set is required when the project does not have exactly "
            f"one evals/*.yaml (found {len(candidates)})",
            context={
                "project": project_dir.name,
                "found": [c.name for c in candidates],
            },
        )
    return candidates[0].resolve()


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.post("/evals", response_model=TaskLaunched, status_code=202)
    async def launch_eval(body: EvalLaunchRequest) -> TaskLaunched:
        assert ctx.tasks is not None

        if body.scope in ("project", "agent"):
            project_dir = ctx.project_dir(body.target)
            spec_path = _resolve_eval_path(ctx, project_dir, body.eval_set)

            async def _run_project(task: StudioTask) -> dict[str, Any]:
                spec = load_eval_spec(spec_path)
                compiled = ctx.compiled(body.target)
                target: Any
                if body.scope == "agent":
                    target = AgentEvalTarget(compiled)
                else:
                    target = ProjectEvalTarget(compiled)
                task.log.append(
                    {"event": "eval.started", "eval_spec": str(spec_path)}
                )
                result = await run_eval(
                    spec,
                    target,
                    transport=ctx.transport,
                    eval_spec_ref=str(spec_path),
                )
                summary = {
                    "eval_run_id": str(result.eval_run_id),
                    "score": result.score,
                    "passed": result.passed,
                    "cases_total": result.cases_total,
                    "cases_passed": result.cases_passed,
                    "fail_under_met": (
                        result.score >= body.fail_under
                        if body.fail_under is not None
                        else True
                    ),
                }
                return summary

            task_id = ctx.tasks.launch("eval", _run_project)
            return TaskLaunched(
                task_id=task_id, events_url=f"/api/tasks/{task_id}/events"
            )

        # tool scope
        roots = FoundryRoots(
            catalog_roots=ctx.catalog_roots() or [ctx.repo_root / "catalog"],
            projects_root=ctx.projects_root,
        )
        if not body.eval_set:
            raise ConfigValidationError(
                "eval_set is required for tool-scope evals",
                context={"target": body.target},
            )
        spec_path_tool = ctx.repo_root / body.eval_set
        if not spec_path_tool.is_file():
            raise ConfigLoadError(
                f"eval set {body.eval_set!r} not found",
                context={"eval_set": body.eval_set, "not_found": True},
            )

        async def _run_tool(task: StudioTask) -> dict[str, Any]:
            spec = load_eval_spec(spec_path_tool)
            target = load_tool_target(body.target, roots)
            task.log.append(
                {"event": "eval.started", "eval_spec": str(spec_path_tool)}
            )
            result = await run_eval(
                spec,
                target,
                transport=ctx.transport,
                eval_spec_ref=str(spec_path_tool),
            )
            return {
                "eval_run_id": str(result.eval_run_id),
                "score": result.score,
                "passed": result.passed,
                "cases_total": result.cases_total,
                "cases_passed": result.cases_passed,
            }

        task_id = ctx.tasks.launch("eval", _run_tool)
        return TaskLaunched(
            task_id=task_id, events_url=f"/api/tasks/{task_id}/events"
        )

    @router.get("/evals", response_model=list[EvalRunRow])
    def list_evals(project: str | None = Query(None)) -> list[EvalRunRow]:
        rows = get_store().eval_rows(project=project)
        return [
            EvalRunRow(
                eval_run_id=str(row.get("eval_run_id", "")),
                eval_name=str(row.get("eval_name", "")),
                project=str(row.get("project", "")),
                target_ref=str(row.get("target_ref", "")),
                target_version=str(row.get("target_version", "")),
                score=float(row.get("score") or 0.0),
                threshold=float(row.get("threshold") or 0.0),
                passed=bool(row.get("passed")),
                completed_at=row.get("completed_at"),
            )
            for row in rows
        ]

    @router.get("/evals/{eval_run_id}")
    def show_eval(eval_run_id: str) -> dict[str, Any]:
        result = load_eval_result(eval_run_id)
        return result.model_dump(mode="json", by_alias=True)

    @router.post("/evals/compare")
    async def compare(body: EvalCompareRequest) -> dict[str, Any]:
        if body.tool and body.versions:
            roots = FoundryRoots(
                catalog_roots=(
                    ctx.catalog_roots() or [ctx.repo_root / "catalog"]
                ),
                projects_root=ctx.projects_root,
            )
            comparison = await compare_tool_versions(
                body.tool,
                body.versions,
                roots,
                eval_path=(
                    ctx.repo_root / body.eval_set if body.eval_set else None
                ),
                transport=ctx.transport,
            )
        elif body.project and body.pin_sets:
            project_dir = ctx.project_dir(body.project)
            eval_path = _resolve_eval_path(ctx, project_dir, body.eval_set)
            comparison = await compare_project_pin_sets(
                project_dir,
                eval_path,
                body.pin_sets,
                transport=ctx.transport,
            )
        else:
            raise ConfigValidationError(
                "compare needs either {tool, versions[]} or "
                "{project, pin_sets[]}",
                context={},
            )
        return comparison.model_dump(mode="json", by_alias=True)

    return router


__all__ = ["build_router"]
