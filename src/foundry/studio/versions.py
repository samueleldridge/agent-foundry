"""Versions / diff / rollback / compute-version (docs/72 § Versions).

Rollback is dry-run-by-default: the plan + pre-flight results always come
back first; a confirmed apply drives the SAME
``foundry.versioning.rollback`` executor the CLI uses (single-file pin
commits, audit entries with ``operator.kind = "studio"``).
"""

from __future__ import annotations

import re
from typing import Any

import yaml
from fastapi import APIRouter, Query, Request

from foundry.config.refs import FoundryRoots, list_versions
from foundry.core.errors import ConfigValidationError, FoundryError
from foundry.deploy.compute_version import compute_system_version
from foundry.studio.context import StudioContext
from foundry.studio.events import emit_studio_event
from foundry.studio.schemas import (
    ArtifactVersions,
    CommitModel,
    ComputeVersionResponse,
    DiffResponse,
    FileDiff,
    PreflightCheckModel,
    RollbackRequest,
    RollbackResponse,
    VersionsResponse,
)
from foundry.studio.security import studio_operator
from foundry.versioning.artifacts import list_prompt_versions, prompts_dir
from foundry.versioning.refs import parse_artifact_ref
from foundry.versioning.rollback import (
    RollbackPlan,
    execute_rollback,
    plan_project_rollback,
    plan_prompt_rollback,
    plan_tool_rollback,
)

_DIFF_FILE_RE = re.compile(r"^diff --git a/(?P<path>\S+) b/\S+", re.MULTILINE)


def _raw_system(ctx: StudioContext, name: str) -> dict[str, Any]:
    data = yaml.safe_load(
        (ctx.project_dir(name) / "system.yaml").read_text()
    )
    return data if isinstance(data, dict) else {}


def _binding_rows(
    ctx: StudioContext, name: str, bindings: Any, kind: str
) -> list[ArtifactVersions]:
    if not isinstance(bindings, dict):
        return []
    roots = FoundryRoots.for_project(ctx.project_dir(name))
    rows: list[ArtifactVersions] = []
    for logical, binding in sorted(bindings.items()):
        if not isinstance(binding, dict):
            continue
        ref_str = str(binding.get("ref", ""))
        pinned = str(binding.get("version", ""))
        try:
            ref = parse_artifact_ref(
                ref_str,
                default_kind=kind,  # type: ignore[arg-type]
                version=pinned,
            )
            versions = list_versions(ref.artifact_dir(roots))
        except FoundryError:
            versions = []
        rows.append(
            ArtifactVersions(
                name=str(logical),
                kind=kind,
                ref=ref_str,
                versions=versions,
                pinned=pinned,
                latest_unpinned=(
                    versions[-1]
                    if versions and versions[-1] != pinned
                    else None
                ),
            )
        )
    return rows


def split_diff(diff_text: str) -> list[FileDiff]:
    """One unified diff → per-file hunks (docs/72: structured diff)."""
    if not diff_text.strip():
        return []
    matches = list(_DIFF_FILE_RE.finditer(diff_text))
    files: list[FileDiff] = []
    for index, match in enumerate(matches):
        end = (
            matches[index + 1].start()
            if index + 1 < len(matches)
            else len(diff_text)
        )
        files.append(
            FileDiff(
                path=match.group("path"),
                hunks=diff_text[match.start():end].rstrip("\n"),
            )
        )
    return files


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/projects/{name}/versions", response_model=VersionsResponse
    )
    def versions(
        name: str, tool: str | None = Query(None)
    ) -> VersionsResponse:
        project_dir = ctx.project_dir(name)
        backend = ctx.backend()
        system = _raw_system(ctx, name)
        tools = _binding_rows(ctx, name, system.get("tools") or {}, "tool")
        if tool is not None:
            tools = [row for row in tools if row.name == tool]
        prompts: list[ArtifactVersions] = []
        agents = system.get("agents") or []
        if isinstance(agents, list):
            for agent in agents:
                agent_dir = project_dir / "agents" / str(agent)
                pinned = ""
                agent_yaml = agent_dir / "agent.yaml"
                if agent_yaml.is_file():
                    data = yaml.safe_load(agent_yaml.read_text())
                    prompt = (
                        data.get("prompt") if isinstance(data, dict) else None
                    )
                    if isinstance(prompt, dict):
                        pinned = str(prompt.get("version", ""))
                versions_list = list_prompt_versions(
                    prompts_dir(project_dir, str(agent))
                )
                prompts.append(
                    ArtifactVersions(
                        name=str(agent),
                        kind="prompt",
                        versions=versions_list,
                        pinned=pinned,
                        latest_unpinned=(
                            versions_list[-1]
                            if versions_list and versions_list[-1] != pinned
                            else None
                        ),
                    )
                )
        rel = backend.relpath(project_dir)
        return VersionsResponse(
            project=name,
            branch=backend.current_branch(),
            commits=[
                CommitModel(
                    sha=commit.sha,
                    short_sha=commit.short_sha,
                    author=commit.author,
                    date=commit.date,
                    subject=commit.subject,
                )
                for commit in backend.log(20, paths=[rel])
            ],
            prompts=prompts,
            tools=tools,
            connections=_binding_rows(
                ctx, name, system.get("connections") or {}, "connection"
            ),
        )

    @router.get("/projects/{name}/diff", response_model=DiffResponse)
    def diff(
        name: str,
        ref1: str = Query(...),
        ref2: str = Query(...),
        path: str | None = Query(None),
    ) -> DiffResponse:
        project_dir = ctx.project_dir(name)
        backend = ctx.backend()
        rel = backend.relpath(project_dir)
        scope = f"{rel}/{path.lstrip('/')}" if path else rel
        return DiffResponse(
            project=name,
            ref1=ref1,
            ref2=ref2,
            files=split_diff(backend.diff(ref1, ref2, paths=[scope])),
        )

    @router.post(
        "/projects/{name}/rollback", response_model=RollbackResponse
    )
    def rollback(
        name: str, body: RollbackRequest, request: Request
    ) -> RollbackResponse:
        project_dir = ctx.project_dir(name)
        backend = ctx.backend()
        if body.tool is not None and body.prompt is not None:
            raise ConfigValidationError(
                "tool and prompt are mutually exclusive (one rollback, "
                "one artifact — docs/52)",
                context={"tool": body.tool, "prompt": body.prompt},
            )
        plan: RollbackPlan
        if body.tool is not None:
            plan = plan_tool_rollback(
                project_dir, body.tool, body.to, backend=backend
            )
            target = f"tool:{body.tool}"
        elif body.prompt is not None:
            plan = plan_prompt_rollback(
                project_dir, body.prompt, body.to, backend=backend
            )
            target = f"prompt:{body.prompt}"
        else:
            plan = plan_project_rollback(
                project_dir, body.to, backend=backend
            )
            target = f"project:{body.to}"
        checks = [
            PreflightCheckModel(
                name=check.name,
                ok=check.ok,
                detail=check.detail,
                bypass=check.bypass,
            )
            for check in plan.checks
        ]
        if body.dry_run:
            return RollbackResponse(
                granularity=plan.granularity,
                target=target,
                dry_run=True,
                plan=plan.render(),
                checks=checks,
            )
        result = execute_rollback(
            plan,
            backend=backend,
            operator=studio_operator(),
            force=body.force,
            assume_yes=True,
        )
        emit_studio_event(
            "studio.rollback",
            project=name,
            studio_request_id=getattr(
                request.state, "studio_request_id", ""
            ),
            mode=plan.granularity,
            target=target,
            commit_sha=result.commit_sha,
        )
        ctx.invalidate(name)
        return RollbackResponse(
            granularity=plan.granularity,
            target=target,
            dry_run=False,
            plan=plan.render(),
            checks=checks,
            commit_sha=result.commit_sha,
            audit_entry_id=str(result.audit_entry.id),
            overrides_used=list(result.overrides_used),
            notes=list(result.notes),
        )

    @router.get(
        "/projects/{name}/compute-version",
        response_model=ComputeVersionResponse,
    )
    def compute_version(name: str) -> ComputeVersionResponse:
        return ComputeVersionResponse(
            project=name,
            system_version=compute_system_version(ctx.project_dir(name)),
        )

    return router


__all__ = ["build_router", "split_diff"]
