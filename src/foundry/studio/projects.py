"""Project discovery, summary, scaffold, and test launch (docs/72 §
Projects)."""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from fastapi import APIRouter

from foundry.config.loader import load_project
from foundry.core.errors import (
    ConfigValidationError,
    FoundryError,
    ProjectUnavailableError,
)
from foundry.observability.events import get_store
from foundry.studio.context import StudioContext
from foundry.studio.schemas import (
    ProjectAgent,
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectDetail,
    ProjectSummary,
    ProjectUnavailableInfo,
)


def _summary(ctx: StudioContext, name: str, branch: str) -> ProjectSummary:
    project_dir = ctx.project_dir(name)
    backend = ctx.backend()
    healthy, detail = True, "config loads"
    agent_count = tool_count = 0
    try:
        loaded = load_project(project_dir)
        agent_count = len(loaded.system.agents)
        tool_count = len(loaded.system.tools)
    except FoundryError as exc:
        healthy, detail = False, str(exc).splitlines()[0]
    commits = backend.log(1, paths=[backend.relpath(project_dir)])
    last_eval: float | None = None
    rows = get_store().eval_rows(project=name)
    if rows:
        last_eval = float(rows[-1].get("score", 0.0))
    return ProjectSummary(
        name=name,
        branch=branch,
        agent_count=agent_count,
        tool_count=tool_count,
        last_commit=commits[0].short_sha if commits else None,
        last_commit_subject=commits[0].subject if commits else None,
        last_eval_score=last_eval,
        healthy=healthy,
        health_detail=detail,
    )


def _unavailable_info(
    ctx: StudioContext, name: str
) -> ProjectUnavailableInfo | None:
    """Probe whether the project compiles in THIS environment. Only the
    missing-runtime-secrets case surfaces here (the UI banners it); other
    compile failures keep their existing surfaces (graph's 422 envelope,
    chat's structured 4xx)."""
    try:
        ctx.compiled(name)
    except ProjectUnavailableError as exc:
        return ProjectUnavailableInfo(
            env_vars=list(exc.env_vars), remedy=exc.remedy
        )
    except FoundryError:
        return None
    return None


def project_detail(ctx: StudioContext, name: str) -> ProjectDetail:
    project_dir = ctx.project_dir(name)
    loaded = load_project(project_dir)
    from foundry.deploy.compute_version import compute_system_version

    agents: list[ProjectAgent] = []
    for agent_name, agent in loaded.agents.items():
        spec = agent.spec
        agents.append(
            ProjectAgent(
                name=agent_name,
                model_binding=(
                    f"{spec.model_binding.provider}/{spec.model_binding.model}"
                ),
                prompt_version=spec.prompt.version,
                tools=[
                    _pin(loaded.system.tools, tool) for tool in spec.tools
                ],
                state_read=list(spec.state_visibility.read),
                state_write=list(spec.state_visibility.write),
            )
        )
    return ProjectDetail(
        name=name,
        description=loaded.system.description,
        flow_pattern=loaded.system.flow.type,
        agents=agents,
        functions=list(loaded.system.functions),
        tools={
            logical: f"{binding.ref}@{binding.version}"
            for logical, binding in loaded.system.tools.items()
        },
        connections={
            logical: f"{binding.ref}@{binding.version}"
            for logical, binding in loaded.system.connections.items()
        },
        guardrails=loaded.system.guardrails.model_dump(mode="json"),
        system_version=compute_system_version(project_dir),
        unavailable=_unavailable_info(ctx, name),
    )


def _pin(bindings: dict[str, Any], logical: str) -> str:
    binding = bindings.get(logical)
    if binding is None:
        return logical
    return f"{binding.ref}@{binding.version}"


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.get("/projects", response_model=list[ProjectSummary])
    def list_projects() -> list[ProjectSummary]:
        names = ctx.project_names()
        branch = ctx.backend().current_branch() if names else ""
        return [_summary(ctx, name, branch) for name in names]

    @router.get("/projects/{name}", response_model=ProjectDetail)
    def get_project(name: str) -> ProjectDetail:
        return project_detail(ctx, name)

    @router.post(
        "/projects", response_model=ProjectCreateResponse, status_code=201
    )
    def create_project(body: ProjectCreateRequest) -> ProjectCreateResponse:
        from foundry.cli.project import execute_project_new

        buffer = io.StringIO()
        with redirect_stdout(buffer), redirect_stderr(buffer):
            code = execute_project_new(
                body.name, projects_root=ctx.projects_root
            )
        if code != 0:
            raise ConfigValidationError(
                f"project scaffold refused: {buffer.getvalue().strip()}",
                context={"name": body.name, "exit_code": code},
            )
        return ProjectCreateResponse(
            name=body.name,
            branch=f"foundry/{body.name}",
            project_dir=str(ctx.projects_root / body.name),
        )

    return router


__all__ = ["build_router", "project_detail"]
