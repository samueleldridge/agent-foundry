"""Project discovery, summary, scaffold, and test launch (docs/72 §
Projects)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request

from foundry.config.loader import load_eval_spec, load_project
from foundry.core.errors import (
    FoundryError,
    ProjectUnavailableError,
)
from foundry.observability.events import get_store
from foundry.studio.context import StudioContext
from foundry.studio.events import emit_studio_event
from foundry.studio.schemas import (
    ProjectAgent,
    ProjectCreateRequest,
    ProjectCreateResponse,
    ProjectDetail,
    ProjectSummary,
    ProjectUnavailableInfo,
)

STARTER_EVAL_TEMPLATE = """\
# Starter eval set scaffolded by foundry studio (project new).
#
# TODO: replace every TODO placeholder with REAL cases before forging.
# The eval set is the forge TARGET: the meta-agent optimises toward it
# and is not allowed to modify it (docs/60 § Eval set immutability).
name: {name}_starter
description: >-
  TODO: describe what a correct output looks like. Starter template:
  three exact-match cases; extend freely (docs/40).
scope: project
target: {name}
cases:
  - id: case_1
    input: {{ question: "TODO: first example input" }}
    expected: {{ answer: "TODO: expected output" }}
  - id: case_2
    input: {{ question: "TODO: second example input" }}
    expected: {{ answer: "TODO: expected output" }}
  - id: case_3
    input: {{ question: "TODO: third example input" }}
    expected: {{ answer: "TODO: expected output" }}
scorers:
  - kind: exact
    name: answer_match
    config: {{ field: answer }}
threshold: 0.9
schema_version: 1
"""


def _bootstrap_summary(ctx: StudioContext, name: str, branch: str) -> ProjectSummary:
    backend = ctx.backend()
    project_dir = ctx.projects_root / name
    commits = backend.log(1, paths=[backend.relpath(project_dir)])
    return ProjectSummary(
        name=name,
        branch=branch,
        last_commit=commits[0].short_sha if commits else None,
        last_commit_subject=commits[0].subject if commits else None,
        healthy=True,
        health_detail="awaiting forge bootstrap (no system.yaml yet)",
        bootstrap=True,
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
    def list_projects(
        include_bootstrap: bool = Query(False),
    ) -> list[ProjectSummary]:
        names = ctx.project_names()
        bootstrap_names = (
            ctx.bootstrap_project_names() if include_bootstrap else []
        )
        branch = (
            ctx.backend().current_branch()
            if names or bootstrap_names
            else ""
        )
        rows = [_summary(ctx, name, branch) for name in names]
        rows += [
            _bootstrap_summary(ctx, name, branch) for name in bootstrap_names
        ]
        return sorted(rows, key=lambda row: row.name)

    @router.get("/projects/{name}", response_model=ProjectDetail)
    def get_project(name: str) -> ProjectDetail:
        return project_detail(ctx, name)

    @router.post(
        "/projects", response_model=ProjectCreateResponse, status_code=201
    )
    def create_project(
        body: ProjectCreateRequest, request: Request
    ) -> ProjectCreateResponse:
        from foundry.cli.project import (
            create_project_skeleton,
            restore_origin_branch,
        )

        # Restore is deferred (restore_branch=False) so the starter-eval
        # commit also lands on foundry/<name>; the finally puts the
        # operator's original branch back regardless.
        skeleton = create_project_skeleton(
            body.name, projects_root=ctx.projects_root, restore_branch=False
        )
        files = ["README.md"]
        eval_rel: str | None = None
        try:
            if body.scaffold_eval:
                eval_rel = _scaffold_starter_eval(ctx, body.name)
                files.append(eval_rel)
        finally:
            restore_origin_branch(
                ctx.backend(), skeleton.origin_branch, skeleton.branch
            )
        emit_studio_event(
            "studio.project_created",
            project=body.name,
            studio_request_id=getattr(
                request.state, "studio_request_id", ""
            ),
            branch=skeleton.branch,
            eval_path=eval_rel or "",
        )
        return ProjectCreateResponse(
            name=body.name,
            branch=skeleton.branch,
            project_dir=str(skeleton.project_dir),
            eval_path=eval_rel,
            eval_repo_path=(
                f"projects/{body.name}/{eval_rel}" if eval_rel else None
            ),
            files=files,
        )

    return router


def _scaffold_starter_eval(ctx: StudioContext, name: str) -> str:
    """Write + validate + commit the starter eval template at
    ``evals/<name>.yaml``.

    The eval is the OPERATOR's artifact: the studio scaffolds it as part
    of the human-initiated project-new action (the meta-agent-shaped
    write sandbox keeps refusing ``evals/`` — docs/60 § Eval set
    immutability), and the loader validates the template before anything
    is committed."""
    project_dir = ctx.project_dir(name, allow_bootstrap=True)
    rel = f"evals/{name}.yaml"
    target = project_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(STARTER_EVAL_TEMPLATE.format(name=name))
    try:
        # The loaders are the single validator (docs/72) — nothing is
        # committed unless the template parses as a real EvalSpec.
        load_eval_spec(target)
    except FoundryError:
        target.unlink(missing_ok=True)
        raise
    backend = ctx.backend()
    backend.commit(
        [target], f"studio({name}): scaffold starter eval template ({rel})"
    )
    return rel


__all__ = ["build_router", "project_detail"]
