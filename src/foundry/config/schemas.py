"""The Pydantic schemas every YAML config validates against.

These schemas ARE the spec (docs/12-config-and-validation.md). A field added
or renamed here is an API change. Every top-level schema uses
``extra="forbid"`` so typos fail at load, not at runtime.

Phase-scoping notes:
- ``AgentSpec`` deliberately omits ``memory`` — that field lands in Phase 2c
  per docs/03. ``semantic_cache`` + ``retrievers`` landed in 2b.
- ``HandoffPolicy`` / ``TerminationRule`` / ``GraphEdge`` are minimal stubs;
  their full behavioural spec lives in docs/30 and lands in Phases 3/7.

Layer note: this module imports ``ModelBinding`` and ``EmbedderBinding`` from
``foundry.providers`` (where docs/11 places them). Those are the only
foundry-internal imports ``foundry.config`` makes beyond ``foundry.core``;
documented as a Phase 1 deviation from docs/12 § What config imports.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from foundry.core import AuthScheme, CredentialsRef, Reducer, RetryPolicy
from foundry.providers import ModelBinding
from foundry.providers.embedders import EmbedderBinding

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


# --- Caching + retrieval bindings (docs/12, Phase 2b) -------------------------------


class SemanticCacheConfig(BaseModel):
    """Opt-in similarity cache for an agent's LLM calls (docs/24 § Layer 2)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    """Set False to keep the config block present for reference while
    disabling the cache. Useful for A/B eval."""
    embedder_binding: EmbedderBinding
    similarity_threshold: float = Field(default=0.95, ge=0.5, le=1.0)
    """Cosine similarity floor for a hit. Higher = stricter. Correctness-
    critical; start high (0.95+) and lower based on eval evidence."""
    ttl_s: int = Field(default=3600, ge=1, le=86400 * 30)
    scope: Literal["agent", "project", "global"] = "agent"
    backend: Literal["in_process", "redis", "pgvector"] = "in_process"
    max_entries: int = Field(default=10000, ge=100)
    """Cap on cache size. LRU eviction."""
    backend_config: dict[str, Any] = Field(default_factory=dict)
    """Backend-specific config (sqlite path, redis url, pgvector table,
    expected 'dimensions', ...). Secrets still via CredentialsRef, not here."""


class RerankerBinding(BaseModel):
    """Optional rerank stage on a RetrieverBinding (docs/25 § Rerankers)."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(pattern=r"^(catalog|local)/[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^v\d+$")
    connection_bindings: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    """Validated against the reranker version's config schema at compile."""
    top_k: int | None = Field(default=None, ge=1, le=200)
    """Reranker's output truncation; None keeps all input docs reordered."""


class RetrieverBinding(BaseModel):
    """One retriever slot on an agent (docs/25 § RetrieverBinding)."""

    model_config = ConfigDict(extra="forbid")

    slot: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    """Name the agent uses to reference this retriever
    (``ctx.retrievers.get(slot)``)."""
    ref: str = Field(pattern=r"^(catalog|local)/[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^v\d+$")
    connection_bindings: dict[str, str] = Field(default_factory=dict)
    config: dict[str, Any] = Field(default_factory=dict)
    """Validated against the retriever version's config schema at compile
    (carries e.g. the dense retriever's embedder_binding). Additive field
    vs the docs/12 sketch — documented in the Phase 2b handoff."""
    reranker: RerankerBinding | None = None
    top_k: int = Field(default=20, ge=1, le=500)
    """Default top_k for retrieval; agent code can override per call."""


# --- Memory (docs/12 § MemoryConfig, docs/26; Phase 2c) ------------------------------


class MemoryWindow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_messages: int | None = Field(default=None, ge=1, le=10000)
    max_tokens: int | None = Field(default=None, ge=1, le=200000)

    @model_validator(mode="after")
    def _exactly_one(self) -> MemoryWindow:
        if (self.max_messages is None) == (self.max_tokens is None):
            raise ValueError(
                "exactly one of max_messages or max_tokens must be set"
            )
        return self


class WorkingMemoryLayerConfig(BaseModel):
    """Recency window over a conversation state field (docs/26 § Working)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["working"] = "working"
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    source_field: str = "messages"
    """State field the layer reads from. Must be a list of FoundryMessage
    or a string; validated at compile against StateSpec.schema."""
    window: MemoryWindow


class EpisodicMemoryLayerConfig(BaseModel):
    """Vector/lexical retrieval over past episodes (docs/26 § Episodic)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["episodic"] = "episodic"
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    retriever_slot: str
    """Must be a slot defined in AgentSpec.retrievers (compile-checked)."""
    top_k: int = Field(default=5, ge=1, le=200)
    relevance_threshold: float = Field(default=0.7, ge=0.0, le=1.0)
    purpose: Literal["query", "document"] = "query"


class SemanticMemoryLayerConfig(BaseModel):
    """Synthesised content in a state field, periodically refreshed by an
    LLM consolidator (docs/26 § Semantic). The consolidator runs on the
    AGENT'S model_binding in v1 — a separate consolidator_model_binding
    override is deferred (Phase 2c handoff)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["semantic"] = "semantic"
    name: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    state_field: str
    """State field holding the synthesised content (typically Markdown).
    Validated at compile against StateSpec.schema + the agent's read AND
    write scope."""
    consolidate_every_n_turns: int | None = Field(default=None, ge=1, le=1000)
    consolidate_on_session_end: bool = False
    consolidator_prompt: str | None = None
    """Prompt file path relative to the agent dir. Required when a
    consolidation trigger is set; existence checked at compile."""
    max_size_tokens: int = Field(default=2000, ge=100, le=50000)

    @model_validator(mode="after")
    def _trigger_needs_prompt(self) -> SemanticMemoryLayerConfig:
        has_trigger = (
            self.consolidate_every_n_turns is not None
            or self.consolidate_on_session_end
        )
        if has_trigger and self.consolidator_prompt is None:
            raise ValueError(
                "a consolidation trigger is set but consolidator_prompt is "
                "missing — the consolidator needs a prompt file (docs/26)"
            )
        return self


MemoryLayerConfig = Annotated[
    WorkingMemoryLayerConfig | EpisodicMemoryLayerConfig | SemanticMemoryLayerConfig,
    Field(discriminator="kind"),
]

MemoryPlacement = Literal[
    "system_prefix", "system_suffix", "messages", "user_message_prefix"
]


class MemoryInjectionRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layer: str
    """Must match a name in MemoryConfig.layers (validated below)."""
    placement: MemoryPlacement
    template: str | None = None
    """Formatting template; variables {content} / {docs} / {messages} match
    the layer's contribution type. None uses per-kind defaults."""
    max_tokens: int | None = Field(default=None, ge=1, le=200000)
    """Per-rule truncation ceiling."""


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    layers: list[MemoryLayerConfig] = Field(min_length=1)
    """Declared order is the prompt-injection order when no explicit
    inject_into_prompt rules are given, and the truncation priority order
    (last-listed truncates first)."""
    inject_into_prompt: list[MemoryInjectionRule] = Field(default_factory=list)
    max_envelope_tokens: int | None = Field(default=None, ge=100, le=200000)
    fail_strict: bool = False
    """False (default): a failed layer contributes empty + warning event.
    True: a failed layer raises MemoryLayerError and aborts the run."""

    @model_validator(mode="after")
    def _names_unique_and_rules_resolve(self) -> MemoryConfig:
        names = [layer.name for layer in self.layers]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(
                f"memory layer names must be unique; duplicated: "
                f"{', '.join(duplicates)}"
            )
        unknown = sorted(
            {rule.layer for rule in self.inject_into_prompt} - set(names)
        )
        if unknown:
            raise ValueError(
                f"inject_into_prompt references unknown layer(s): "
                f"{', '.join(unknown)} (declared: {', '.join(names)})"
            )
        return self


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
    """docs/12 § AgentSpec — full cumulative Phase 2 shape."""

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
    semantic_cache: SemanticCacheConfig | None = None
    """Opt-in similarity-based cache for this agent's LLM calls.
    None = disabled (default). See docs/24."""
    retrievers: list[RetrieverBinding] = Field(default_factory=list)
    """Retrievers available to this agent (tool-style via ctx.retrievers or
    pre-agent retrieval). See docs/25."""
    memory: MemoryConfig | None = None
    """Opt-in multi-layer memory (working / episodic / semantic).
    None = no memory subsystem; the agent's prompt is built directly from
    state. Default for batch / one-shot agents. See docs/26."""
    metadata: dict[str, Any] = Field(default_factory=dict)
    schema_version: Literal[1] = 1

    @model_validator(mode="after")
    def _retriever_slots_unique(self) -> AgentSpec:
        slots = [binding.slot for binding in self.retrievers]
        duplicates = sorted({s for s in slots if slots.count(s) > 1})
        if duplicates:
            raise ValueError(
                f"retriever slots must be unique; duplicated: "
                f"{', '.join(duplicates)}"
            )
        return self


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
    """docs/12 § ToolSpec."""

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
    cacheable: bool = False
    """Author's declaration that identical validated input yields the same
    output (within cache_ttl_s). Off by default: silently caching a
    non-idempotent tool is a correctness bug (docs/24 § Layer 3)."""
    cache_ttl_s: int | None = Field(default=None, ge=1, le=86400 * 30)
    """Entry lifetime in seconds. Required when cacheable=True."""
    cache_scope: Literal["agent", "project", "global"] = "project"
    tags: list[str] = Field(default_factory=list)
    standalone_eval: str | None = "eval.yaml"
    connections_required: list[ConnectionSlot] = Field(default_factory=list)
    author: str | None = None
    created_at: datetime | None = None
    schema_version: Literal[1] = 1

    @model_validator(mode="after")
    def _cache_fields_consistent(self) -> ToolSpec:
        if self.cacheable and self.cache_ttl_s is None:
            raise ValueError(
                "cacheable tools must set cache_ttl_s (cacheable: true "
                "without a TTL is rejected at load — docs/24 § Layer 3)"
            )
        if not self.cacheable and self.cache_ttl_s is not None:
            raise ValueError("cache_ttl_s requires cacheable=True")
        return self


# --- RetrieverSpec (catalog/local retriever + reranker artifacts) --------------------


class RetrieverSpec(BaseModel):
    """Shape of a retriever version's retriever.yaml (docs/25 § Catalog
    template details). Reranker artifacts share the shape with
    ``kind: reranker`` — they resolve through the same ``retriever``
    ArtifactRef kind and live under ``<root>/retrievers/``."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^v\d+$")
    description: str
    kind: Literal["dense", "sparse", "hybrid", "reranker"]
    config_schema: str
    """'schemas.py::ClassName' — the config model RetrieverBinding.config /
    RerankerBinding.config validates against."""
    factory: str
    """'factory.py::build_retriever' (or build_reranker) — async factory that
    returns the concrete Retriever/Reranker."""
    connections_required: list[ConnectionSlot] = Field(default_factory=list)
    health_check: str | None = "health.yaml"
    tags: list[str] = Field(default_factory=list)
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
    """Stable, human-readable. The key for per-case results across runs;
    renaming a case loses cross-run comparability (docs/40 § EvalCase)."""
    input: dict[str, Any]
    """Validated against the target's input schema at eval-load time —
    tool: input_schema; agent: the agent's read-scope fields; project:
    the project's state schema."""
    expected: Any
    """Shape depends on the scorers: exact wants a value or partial
    structure; numeric a comparison target; llm_judge/rubric structured
    expectations. Passed to every scorer alongside the actual output."""
    tags: list[str] = Field(default_factory=list)
    weight: float = Field(default=1.0, ge=0.0)
    seed: int | None = None
    """Reserved for a per-case seed override. Accepted by the schema but
    NOT yet consumed: the harness propagates only the spec-level seed to
    providers (Phase 4 limitation, docs/_phase_handoffs/phase_4.md
    deviation 3). Setting it today has no effect on the run."""
    skip: bool = False
    skip_reason: str | None = None
    """Marked but not run — keeps the case in version control while
    debugging without losing it."""


class ScorerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["exact", "numeric", "llm_judge", "rubric", "user"]
    name: str
    """Instance name in reports. For ``kind: user`` this is ALSO the
    Python entry-point name looked up in the ``foundry.scorers`` group
    (docs/40 § user)."""
    config: dict[str, Any] = Field(default_factory=dict)
    """Validated against the scorer kind's config model when the harness
    builds the scorer (load time, before any case runs)."""
    weight: float = Field(default=1.0, ge=0.0)


class EvalSpec(BaseModel):
    """One eval set (docs/40). Determinism contract: with
    ``deterministic: true`` the harness seeds Python ``random``, forces
    ``temperature: 0`` on the target's model binding, and propagates
    ``seed`` to providers that support it — same spec + same target +
    same seed reproduces the same score within scorer-type tolerance
    (exact/numeric: exact; llm_judge: best-effort, flagged
    ``is_deterministic: false`` in results). With ``deterministic:
    false`` each case runs ``replicates`` times and the mean is scored."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    scope: Literal["tool", "agent", "project", "connection", "retriever"]
    """'connection' scope is the shape of a connection's health.yaml
    (docs/23 § Health checks); 'retriever' the shape of a retriever
    template's health.yaml (docs/25 § Catalog template details)."""
    target: str
    cases: list[EvalCase] = Field(min_length=1)
    scorers: list[ScorerConfig] = Field(min_length=1)
    threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    """A case passes when its weighted scorer score >= threshold; the
    eval passes when the case-weighted aggregate >= threshold."""
    max_parallel: int = Field(default=4, ge=1, le=64)
    deterministic: bool = True
    seed: int | None = None
    """Seed applied run-wide in deterministic mode (propagated to the
    provider where the model supports ``seed``; best-effort otherwise,
    surfaced as a warning)."""
    replicates: int = Field(default=1, ge=1, le=20)
    """Non-deterministic mode only: run each case N times and report the
    mean score (per-replicate scores land in the case metadata)."""
    case_timeout_s: float = Field(default=300.0, gt=0)
    """Hard wall-clock cap per case; a case that exceeds it errors with
    score 0.0 and the run continues."""
    case_max_cost_usd: Decimal | None = None
    """Per-case Session.cost_budget; a breach errors the case."""
    max_total_cost_usd: Decimal | None = None
    """Across all cases; when hit the run halts and remaining cases are
    marked skipped (partial result reported)."""
    schema_version: Literal[1] = 1

    @model_validator(mode="after")
    def _validate_weights_and_cases(self) -> EvalSpec:
        total = sum(s.weight for s in self.scorers)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"scorer weights must sum to 1.0 (docs/40); got {total} "
                f"across {len(self.scorers)} scorer(s)"
            )
        case_ids = [case.id for case in self.cases]
        duplicates = sorted({i for i in case_ids if case_ids.count(i) > 1})
        if duplicates:
            raise ValueError(
                f"case ids must be unique; duplicated: {', '.join(duplicates)}"
            )
        return self


__all__ = [
    "AgentSpec",
    "ConnectionBinding",
    "ConnectionSlot",
    "ConnectionSpec",
    "EmbedderBinding",
    "EpisodicMemoryLayerConfig",
    "EvalCase",
    "EvalSpec",
    "FieldSpec",
    "FlowSpec",
    "FunctionNodeSpec",
    "GraphEdge",
    "GraphFlow",
    "Guardrails",
    "HandoffPolicy",
    "MemoryConfig",
    "MemoryInjectionRule",
    "MemoryLayerConfig",
    "MemoryPlacement",
    "MemoryWindow",
    "ObservabilityConfig",
    "OutputSchemaRef",
    "ParallelFlow",
    "PoolPolicy",
    "PromptRef",
    "RefreshPolicy",
    "RerankerBinding",
    "RetrieverBinding",
    "RetrieverSpec",
    "ScorerConfig",
    "SemanticCacheConfig",
    "SemanticMemoryLayerConfig",
    "SequentialFlow",
    "SingleFlow",
    "StateSpec",
    "StateVisibility",
    "SupervisorFlow",
    "SystemSpec",
    "TerminationRule",
    "ToolBinding",
    "ToolSpec",
    "WorkingMemoryLayerConfig",
]
