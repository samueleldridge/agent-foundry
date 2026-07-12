"""Per-project request/response models introspected from the CompiledProject.

docs/70 § Endpoint generation algorithm — no hand-written per-project
routes:

- **ProjectInput** — the state fields the flow's START node(s) read that
  have no default (i.e. the required fields of the compiled state model).
  Same CompiledProject → same model → same OpenAPI schema (deterministic).
- **ProjectOutput** — the terminal agent's ``output_schema`` (the
  CompiledProject's primary-agent mirror). Sequential pipelines are the
  documented exception: their run product IS the final state (a
  post-agent function node may have transformed the agent's output), so
  the response model is the compiled state model.

Also the fixed wire models: RunStatusResponse, Health, ConfigSnapshot.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, create_model

from foundry.orchestration.patterns import (
    GraphPlan,
    LeafNode,
    ParallelPlan,
    PlanNode,
    SequentialPlan,
    SupervisorPlan,
)
from foundry.runtime.compiled import CompiledProject


def _start_nodes(node: PlanNode) -> list[str]:
    """The node(s) that receive the seeded state first (docs/70: the
    'start node' whose reads define the project input)."""
    if isinstance(node, LeafNode):
        return [node.name]
    if isinstance(node, SequentialPlan):
        return _start_nodes(node.steps[0])
    if isinstance(node, ParallelPlan):
        names: list[str] = []
        for branch in node.branches:
            names.extend(_start_nodes(branch))
        return names
    if isinstance(node, SupervisorPlan):
        return [node.supervisor]
    if isinstance(node, GraphPlan):
        return [node.start]
    raise TypeError(f"unknown plan node {type(node).__name__}")


def _title(project: str, suffix: str) -> str:
    return "".join(part.title() for part in project.split("_")) + suffix


def derive_input_model(compiled: CompiledProject) -> type[BaseModel]:
    """State fields without defaults that are read by the start node(s)
    → a Pydantic request model (docs/70 § Endpoint generation)."""
    plan = compiled.flow_plan()
    views = compiled.compiled_state.agent_views
    state_model = compiled.compiled_state.model
    field_types = compiled.compiled_state.field_types
    required: dict[str, Any] = {}
    for node_name in _start_nodes(plan.root):
        view = views.get(node_name)
        if view is None:  # synthetic single-agent projects (meta-agent)
            continue
        for field_name in view.read:
            model_field = state_model.model_fields.get(field_name)
            if model_field is not None and model_field.is_required():
                required[field_name] = (field_types[field_name], ...)
    model: type[BaseModel] = create_model(
        _title(compiled.project.system.name, "Input"),
        __config__=ConfigDict(extra="forbid"),
        **required,
    )
    return model


def derive_output_model(compiled: CompiledProject) -> type[BaseModel]:
    """Terminal agent's output schema; sequential pipelines return the
    compiled state model (the run product is the final state)."""
    if compiled.flow_steps:
        return compiled.compiled_state.model
    return compiled.output_model


# --- fixed wire models -----------------------------------------------------------


class PendingApproval(BaseModel):
    model_config = ConfigDict(extra="allow")

    approval_id: str
    prompt: str = ""
    context: dict[str, Any] = Field(default_factory=dict)
    agent_name: str = ""
    tool_ref: str | None = None


class RunStatusResponse(BaseModel):
    """GET /runs/{run_id} (docs/70 § Run status)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    project: str
    system_version: str = ""
    status: str
    started_at: datetime | None = None
    completed_at: datetime | None = None
    current_node: str | None = None
    tokens_used: int = 0
    cost_so_far_usd: str | None = None
    pending_approval: PendingApproval | None = None
    error: dict[str, Any] | None = None
    events_url: str = ""


class DependencyHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    latency_ms: int = 0
    error: str | None = None


class Health(BaseModel):
    """GET /health (docs/70 § Health): liveness is cheap; ?deep=true adds
    dependency checks and flips to 503 when degraded or draining."""

    model_config = ConfigDict(extra="forbid")

    status: str
    uptime_s: int
    worker_id: str
    project: str
    checkpointer: DependencyHealth | None = None
    rate_limiter: DependencyHealth | None = None


class ConfigSnapshot(BaseModel):
    """GET /config (docs/70 § Config endpoint) — redacted: names +
    versions + guardrails only; never secrets, raw config text, or
    handler bodies."""

    model_config = ConfigDict(extra="forbid")

    project: str
    system_version: str
    pin_set_hash: str
    framework_version: str
    agents: list[str]
    functions: list[str]
    flow_pattern: str
    tools_pinned: dict[str, str]
    connections: dict[str, dict[str, str]]
    guardrails: dict[str, Any]
    compiled_at: datetime


class RunAccepted(BaseModel):
    """409 body when a non-streaming run pauses on an approval
    (docs/70 § POST /run status codes)."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: str
    pending_approval: PendingApproval | None = None
    resume_url: str = ""


__all__ = [
    "ConfigSnapshot",
    "DependencyHealth",
    "Health",
    "PendingApproval",
    "RunAccepted",
    "RunStatusResponse",
    "derive_input_model",
    "derive_output_model",
]
