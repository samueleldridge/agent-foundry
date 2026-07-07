"""The Pydantic schemas every YAML config validates against.

These schemas ARE the spec (docs/12-config-and-validation.md). A field added
or renamed here is an API change. Every top-level schema uses
``extra="forbid"`` so typos fail at load, not at runtime.

Phase-scoping notes:
- ``AgentSpec`` deliberately omits ``semantic_cache`` / ``retrievers`` /
  ``memory`` — those fields land in Phases 2b/2c per docs/03.
- ``ToolSpec`` omits the cache fields (``cacheable`` / ``cache_ttl_s`` /
  ``cache_scope``) — Phase 2b.
- ``HandoffPolicy`` / ``TerminationRule`` / ``GraphEdge`` are minimal stubs;
  their full behavioural spec lives in docs/30 and lands in Phases 3/7.

Layer note: this module imports ``ModelBinding`` from ``foundry.providers``
(where docs/11 places it). That is the one foundry-internal import
``foundry.config`` makes beyond ``foundry.core``; documented as a Phase 1
deviation from docs/12 § What config imports.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from foundry.core import AuthScheme, CredentialsRef, Reducer, RetryPolicy
from foundry.providers import ModelBinding

# --- Flow (discriminated union over pattern types) ---------------------------


class HandoffPolicy(BaseModel):
    """Stub — full spec in docs/30 (Phase 7)."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["llm", "rule", "hybrid"] = "llm"


class TerminationRule(BaseModel):
    """Stub — full spec in docs/30 (Phase 7)."""

    model_config = ConfigDict(extra="forbid")

    max_hops: int = Field(default=20, ge=1, le=1000)


class GraphEdge(BaseModel):
    """Stub — full spec in docs/30 (Phase 3/7)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    from_: str = Field(alias="from")
    to: str
    when: str | None = None


class SingleFlow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["single"] = "single"
    agent: str


class SequentialFlow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["sequential"] = "sequential"
    steps: list[str] = Field(min_length=1)


class ParallelFlow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["parallel"] = "parallel"
    parallel_branches: list[str] = Field(min_length=2)
    join: str | None = None
    then: list[str] = Field(default_factory=list)


class SupervisorFlow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["supervisor"] = "supervisor"
    supervisor: str
    workers: list[str] = Field(min_length=1)
    handoff_policy: HandoffPolicy = Field(default_factory=HandoffPolicy)
    termination: TerminationRule = Field(default_factory=TerminationRule)


class GraphFlow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["graph"] = "graph"
    start: str
    edges: list[GraphEdge] = Field(min_length=1)


FlowSpec = Annotated[
    SingleFlow | SequentialFlow | ParallelFlow | SupervisorFlow | GraphFlow,
    Field(discriminator="type"),
]


# --- Guardrails + observability ----------------------------------------------


class Guardrails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_iterations: int = Field(default=10, ge=1, le=1000)
    max_hops: int = Field(default=20, ge=1, le=1000)
    max_cost_usd: Decimal | None = None
    max_wall_time_s: float | None = None


class ObservabilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace: Literal["otel", "langsmith", "off"] = "otel"
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    capture_inputs: bool = True
    capture_outputs: bool = True
    capture_tool_args: bool = True


# --- SystemSpec ----------------------------------------------------------------


class ToolBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(pattern=r"^(catalog|local)/[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^v\d+$")
    settings: dict[str, Any] = Field(default_factory=dict)
    connection_bindings: dict[str, str] = Field(default_factory=dict)


class ConnectionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(pattern=r"^(catalog|local)/[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^v\d+$")
    config: dict[str, Any] = Field(default_factory=dict)
    credentials_ref: CredentialsRef
    refresh_overrides: RefreshPolicy | None = None
    pool_overrides: PoolPolicy | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class SystemSpec(BaseModel):
    """The project manifest — one per project (docs/12 § SystemSpec)."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    description: str
    agents: list[str] = Field(default_factory=list)
    functions: list[str] = Field(default_factory=list)
    state: str = "state.yaml"
    flow: FlowSpec
    tools: dict[str, ToolBinding] = Field(default_factory=dict)
    connections: dict[str, ConnectionBinding] = Field(default_factory=dict)
    guardrails: Guardrails = Field(default_factory=Guardrails)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    metadata: dict[str, Any] = Field(default_factory=dict)
    schema_version: Literal[1] = 1

    @model_validator(mode="after")
    def _at_least_one_node(self) -> SystemSpec:
        if not self.agents and not self.functions:
            raise ValueError("at least one of agents or functions must be non-empty")
        return self


# --- StateSpec -------------------------------------------------------------------


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str
    default: Any | None = None
    description: str = ""


class StateVisibility(BaseModel):
    model_config = ConfigDict(extra="forbid")

    read: list[str] = Field(default_factory=list)
    write: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _no_nameless(self) -> StateVisibility:
        if not self.read and not self.write:
            raise ValueError("must declare at least one of read or write")
        return self


class StateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    state_schema: dict[str, FieldSpec] = Field(alias="schema")
    reducers: dict[str, Reducer] = Field(default_factory=dict)
    visibility: dict[str, StateVisibility] = Field(default_factory=dict)
    schema_version: Literal[1] = 1


# --- AgentSpec ---------------------------------------------------------------------


class PromptRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^v\d+$")
    path: str

    @model_validator(mode="after")
    def _check_consistency(self) -> PromptRef:
        if not self.path.endswith(f"{self.version}.md"):
            raise ValueError(
                f"path {self.path!r} does not match version {self.version!r} "
                f"(must end with '{self.version}.md')"
            )
        return self


class OutputSchemaRef(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    schema_ref: str = Field(alias="schema")
    """'output_schema.py::ClassName' — loaded by importlib at compile time,
    relative to the agent directory."""


class AgentSpec(BaseModel):
    """docs/12 § AgentSpec, minus the 2b/2c fields (semantic_cache,
    retrievers, memory)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    description: str = ""
    model_binding: ModelBinding
    prompt: PromptRef
    output: OutputSchemaRef
    tools: list[str] = Field(default_factory=list)
    state_visibility: StateVisibility
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    iteration_limit: int = Field(default=20, ge=1, le=500)
    metadata: dict[str, Any] = Field(default_factory=dict)
    schema_version: Literal[1] = 1


# --- FunctionNodeSpec -----------------------------------------------------------


class FunctionNodeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    description: str = ""
    function: str
    state_visibility: StateVisibility
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_s: float = Field(default=30.0, gt=0)
    metadata: dict[str, Any] = Field(default_factory=dict)
    schema_version: Literal[1] = 1


# --- ToolSpec ----------------------------------------------------------------------


class ConnectionSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    accepts: list[str] = Field(min_length=1)
    description: str = ""
    optional: bool = False


class ToolSpec(BaseModel):
    """docs/12 § ToolSpec, minus the cache fields (Phase 2b)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^v\d+$")
    description: str
    input_schema: str
    output_schema: str
    handler: str
    timeout_s: float = Field(default=30.0, gt=0)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    overridable_settings: list[str] = Field(
        default_factory=lambda: ["timeout_s", "retry_policy"]
    )
    tags: list[str] = Field(default_factory=list)
    standalone_eval: str | None = "eval.yaml"
    connections_required: list[ConnectionSlot] = Field(default_factory=list)
    author: str | None = None
    created_at: datetime | None = None
    schema_version: Literal[1] = 1


# --- ConnectionSpec ----------------------------------------------------------------


class RefreshPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "expiry", "periodic", "on_auth_error"] = "expiry"
    refresh_interval_s: int | None = None
    early_refresh_buffer_s: int = 60


class PoolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent: int = Field(default=32, ge=1, le=1024)
    idle_ttl_s: int | None = None
    acquire_timeout_s: float = 30.0


class ConnectionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^v\d+$")
    description: str
    auth_scheme: AuthScheme
    config_schema: str
    factory: str
    client_type: str = ""
    health_check: str | None = "health.yaml"
    refresh: RefreshPolicy = Field(default_factory=RefreshPolicy)
    pool: PoolPolicy = Field(default_factory=PoolPolicy)
    non_sensitive_config_fields: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    author: str | None = None
    created_at: datetime | None = None
    schema_version: Literal[1] = 1


# --- EvalSpec ------------------------------------------------------------------------


class EvalCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    input: dict[str, Any]
    expected: Any
    tags: list[str] = Field(default_factory=list)
    weight: float = Field(default=1.0, ge=0.0)


class ScorerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["exact", "llm_judge", "rubric", "user"]
    name: str
    config: dict[str, Any] = Field(default_factory=dict)
    weight: float = 1.0


class EvalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    scope: Literal["tool", "agent", "project", "connection"]
    """'connection' scope is the shape of a connection's health.yaml
    (docs/23 § Health checks)."""
    target: str
    cases: list[EvalCase] = Field(min_length=1)
    scorers: list[ScorerConfig] = Field(min_length=1)
    threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    max_parallel: int = Field(default=4, ge=1, le=64)
    deterministic: bool = True
    seed: int | None = None
    schema_version: Literal[1] = 1


__all__ = [
    "AgentSpec",
    "ConnectionBinding",
    "ConnectionSlot",
    "ConnectionSpec",
    "EvalCase",
    "EvalSpec",
    "FieldSpec",
    "FlowSpec",
    "FunctionNodeSpec",
    "GraphEdge",
    "GraphFlow",
    "Guardrails",
    "HandoffPolicy",
    "ObservabilityConfig",
    "OutputSchemaRef",
    "ParallelFlow",
    "PoolPolicy",
    "PromptRef",
    "RefreshPolicy",
    "ScorerConfig",
    "SequentialFlow",
    "SingleFlow",
    "StateSpec",
    "StateVisibility",
    "SupervisorFlow",
    "SystemSpec",
    "TerminationRule",
    "ToolBinding",
    "ToolSpec",
]
