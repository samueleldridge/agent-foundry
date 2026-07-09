"""RunEvent + InboundMessage tagged unions.

Every streaming surface (CLI ``--stream``, API SSE/WebSocket, run-artifact
writer, meta-agent session) serialises from this shape. See docs/10
§ Streaming events for the full event catalogue.

Phase 1 emits only a tiny subset (run.started, agent.started/completed,
llm.completed, run.completed, run.failed). The full union is defined here so
downstream consumers can pattern-match exhaustively from day one.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from foundry.core.connection import ConnectionDescriptor
from foundry.core.messages import FoundryMessage, TextBlock
from foundry.core.model import StopReason, TokenUsage, ToolUseBlockDelta
from foundry.core.types import RunId


class _RunEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: RunId
    sequence: int
    timestamp: datetime


class RunStarted(_RunEventBase):
    event: Literal["run.started"] = "run.started"
    project: str
    system_version: str
    pin_set_hash: str
    inputs_hash: str


class AgentStarted(_RunEventBase):
    event: Literal["agent.started"] = "agent.started"
    agent_name: str
    agent_version: str


class AgentCompleted(_RunEventBase):
    event: Literal["agent.completed"] = "agent.completed"
    agent_name: str
    output_summary: str | None = None


class FunctionNodeStarted(_RunEventBase):
    event: Literal["function_node.started"] = "function_node.started"
    node_name: str
    node_version: str


class FunctionNodeCompleted(_RunEventBase):
    event: Literal["function_node.completed"] = "function_node.completed"
    node_name: str
    node_version: str = ""
    fields_written: list[str] = Field(default_factory=list)
    bytes_delta: int = 0
    latency_ms: int = 0


class LLMCallStarted(_RunEventBase):
    event: Literal["llm.started"] = "llm.started"
    agent_name: str
    provider: str
    model: str
    prompt_tokens_estimate: int | None = None
    prompt_messages: list[FoundryMessage] | None = None
    """The assembled prompt, captured ONLY when
    ObservabilityConfig.capture_inputs is true (docs/26 § Observability:
    'with capture_inputs: true, the assembled prompt that followed')."""


class LLMDelta(_RunEventBase):
    event: Literal["llm.delta"] = "llm.delta"
    agent_name: str
    content_block_index: int
    delta: TextBlock | ToolUseBlockDelta | None = None


class LLMCallCompleted(_RunEventBase):
    event: Literal["llm.completed"] = "llm.completed"
    agent_name: str
    usage: TokenUsage
    cost_estimate_usd: Decimal | None = None
    latency_ms: int = 0
    stop_reason: StopReason


class ToolStarted(_RunEventBase):
    event: Literal["tool.started"] = "tool.started"
    agent_name: str
    tool_ref: str
    tool_version: str
    input_hash: str
    input_preview: str | None = None


class ToolCompleted(_RunEventBase):
    event: Literal["tool.completed"] = "tool.completed"
    agent_name: str
    tool_ref: str
    tool_version: str
    success: bool
    latency_ms: int = 0
    retry_count: int = 0
    output_preview: str | None = None
    error_category: str | None = None


class ConnectionEvent(_RunEventBase):
    event: Literal["connection"] = "connection"
    agent_name: str
    connection_descriptor: ConnectionDescriptor
    lifecycle: Literal[
        "acquire", "cache_hit", "refresh", "release", "evict", "health_check"
    ]
    latency_ms: int = 0


class EmbedCall(_RunEventBase):
    event: Literal["embed"] = "embed"
    agent_name: str
    embedder: str
    input_count: int
    input_tokens: int
    purpose: Literal["query", "document"]
    latency_ms: int = 0
    cost_estimate_usd: Decimal | None = None


class SemanticCacheHitEvent(_RunEventBase):
    event: Literal["cache.semantic.hit"] = "cache.semantic.hit"
    agent_name: str
    similarity: float
    threshold: float
    cached_at: datetime
    saved_tokens_estimate: int
    saved_cost_estimate_usd: Decimal | None = None


class SemanticCacheMiss(_RunEventBase):
    event: Literal["cache.semantic.miss"] = "cache.semantic.miss"
    agent_name: str
    top_similarity: float
    threshold: float


class SemanticCacheStore(_RunEventBase):
    event: Literal["cache.semantic.store"] = "cache.semantic.store"
    agent_name: str
    ttl_s: int


class SemanticCacheInvalidate(_RunEventBase):
    """Emitted when an agent-version change (prompt / tool-binding / model-
    binding edit) evicts that agent's entries (docs/24 correctness rule 1)."""

    event: Literal["cache.semantic.invalidate"] = "cache.semantic.invalidate"
    agent_name: str
    reason: str = "agent_version_changed"
    previous_version: str | None = None
    current_version: str | None = None


class ToolCacheHit(_RunEventBase):
    event: Literal["cache.tool.hit"] = "cache.tool.hit"
    agent_name: str
    tool_ref: str
    tool_version: str
    cached_at: datetime


class ToolCacheMiss(_RunEventBase):
    event: Literal["cache.tool.miss"] = "cache.tool.miss"
    agent_name: str
    tool_ref: str
    tool_version: str


class ToolCacheStore(_RunEventBase):
    event: Literal["cache.tool.store"] = "cache.tool.store"
    agent_name: str
    tool_ref: str
    tool_version: str
    ttl_s: int


class WarningEvent(_RunEventBase):
    """Loud-but-non-fatal degradation: cache fail-open, hybrid branch down,
    reranker fall-through. The run continues; the audit trail records why it
    took the degraded path."""

    event: Literal["warning"] = "warning"
    agent_name: str
    category: str
    """Dotted category, e.g. 'cache.semantic.error', 'retrieval.branch_failed',
    'rerank.fallthrough'."""
    message: str
    error_class: str | None = None


class RetrievalEvent(_RunEventBase):
    event: Literal["retrieval"] = "retrieval"
    agent_name: str
    retriever: str
    kind: Literal["dense", "sparse", "hybrid"]
    top_k: int
    returned: int
    latency_ms: int = 0
    branch_latency_ms: dict[str, int] = Field(default_factory=dict)
    """Hybrid only: per-branch latencies ('dense'/'sparse'). Both non-zero and
    overlapping in wall time proves the branches ran in parallel."""
    branches_failed: list[str] = Field(default_factory=list)


class RerankEvent(_RunEventBase):
    event: Literal["rerank"] = "rerank"
    agent_name: str
    reranker: str
    candidates: int
    top_k: int | None = None
    latency_ms: int = 0
    cost_estimate_usd: Decimal | None = None
    before_ids: list[str] = Field(default_factory=list)
    after_ids: list[str] = Field(default_factory=list)


class MemoryRead(_RunEventBase):
    event: Literal["memory.read"] = "memory.read"
    agent_name: str
    layers_read: list[str] = Field(default_factory=list)
    layers_failed: list[str] = Field(default_factory=list)
    total_tokens_estimate: int = 0
    truncated: bool = False
    layers_truncated: list[str] = Field(default_factory=list)
    """Which layers lost content to the max_envelope_tokens cap —
    last-listed truncates first (docs/26 § Prompt assembly rule 6)."""


class MemoryWriteEvent(_RunEventBase):
    event: Literal["memory.write"] = "memory.write"
    agent_name: str
    layer_name: str
    layer_kind: Literal["working", "episodic", "semantic", "custom"]
    write_kind: Literal["message", "summary", "fact", "raw"]
    bytes: int = 0


class MemoryConsolidate(_RunEventBase):
    event: Literal["memory.consolidate"] = "memory.consolidate"
    agent_name: str
    layer_name: str
    trigger: Literal["periodic", "session_end", "explicit"]
    input_tokens_summarised: int = 0
    output_tokens_written: int = 0
    latency_ms: int = 0


class Handoff(_RunEventBase):
    event: Literal["handoff"] = "handoff"
    from_agent: str
    to_agent: str
    trigger: Literal["rule", "llm", "end"]
    hop_number: int


class StateTransition(_RunEventBase):
    event: Literal["state.transition"] = "state.transition"
    agent_name: str
    fields_written: list[str] = Field(default_factory=list)
    bytes_delta: int = 0


class ApprovalRequiredEvent(_RunEventBase):
    event: Literal["approval.required"] = "approval.required"
    agent_name: str
    approval_id: str
    prompt: str
    context: dict[str, Any] = Field(default_factory=dict)


class ApprovalResolved(_RunEventBase):
    event: Literal["approval.resolved"] = "approval.resolved"
    approval_id: str
    decision: Literal["approved", "rejected"]
    reason: str | None = None


class RunCompleted(_RunEventBase):
    event: Literal["run.completed"] = "run.completed"
    status: Literal["success", "max_hops", "approval_pending"] = "success"
    final_output: Any | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cost_estimate_usd: Decimal | None = None
    duration_ms: int = 0


class RunFailed(_RunEventBase):
    event: Literal["run.failed"] = "run.failed"
    error: dict[str, Any]


class RunCancelledEvent(_RunEventBase):
    event: Literal["run.cancelled"] = "run.cancelled"
    reason: str


# --- forge (meta-agent session) events (docs/62 § Composition) --------------


class ForgeStarted(_RunEventBase):
    event: Literal["forge.started"] = "forge.started"
    project: str
    forge_run_id: str
    meta_agent_version: str
    max_iterations: int
    max_cost_usd: Decimal | None = None
    threshold: float


class ForgeIterationStarted(_RunEventBase):
    event: Literal["forge.iteration_started"] = "forge.iteration_started"
    forge_run_id: str
    iteration_number: int
    directive_kind: Literal["bootstrap", "iterate"]


class ForgeIterationCompleted(_RunEventBase):
    event: Literal["forge.iteration_completed"] = "forge.iteration_completed"
    forge_run_id: str
    iteration_number: int
    eval_score: float | None = None
    eval_delta: float | None = None
    commit_shas: list[str] = Field(default_factory=list)
    cluster_id: str | None = None
    applied: bool = True


class ForgeRollback(_RunEventBase):
    event: Literal["forge.rollback"] = "forge.rollback"
    forge_run_id: str
    iteration_number: int
    scope: str
    target: str
    to_version: str


class ForgeTerminated(_RunEventBase):
    event: Literal["forge.terminated"] = "forge.terminated"
    forge_run_id: str
    reason: str
    final_score: float | None = None
    iterations: int = 0
    total_cost_usd: Decimal | None = None


class MetaAgentViolation(_RunEventBase):
    event: Literal["meta_agent.violation"] = "meta_agent.violation"
    forge_run_id: str
    tool: str
    detail: str


RunEvent = Annotated[
    RunStarted
    | AgentStarted
    | AgentCompleted
    | FunctionNodeStarted
    | FunctionNodeCompleted
    | LLMCallStarted
    | LLMDelta
    | LLMCallCompleted
    | ToolStarted
    | ToolCompleted
    | ConnectionEvent
    | EmbedCall
    | SemanticCacheHitEvent
    | SemanticCacheMiss
    | SemanticCacheStore
    | SemanticCacheInvalidate
    | ToolCacheHit
    | ToolCacheMiss
    | ToolCacheStore
    | WarningEvent
    | RetrievalEvent
    | RerankEvent
    | MemoryRead
    | MemoryWriteEvent
    | MemoryConsolidate
    | Handoff
    | StateTransition
    | ApprovalRequiredEvent
    | ApprovalResolved
    | ForgeStarted
    | ForgeIterationStarted
    | ForgeIterationCompleted
    | ForgeRollback
    | ForgeTerminated
    | MetaAgentViolation
    | RunCompleted
    | RunFailed
    | RunCancelledEvent,
    Field(discriminator="event"),
]


# --- InboundMessage (WebSocket → server) ----------------------------------


class _InboundBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: RunId
    client_sequence: int


class InjectInput(_InboundBase):
    kind: Literal["inject_input"] = "inject_input"
    message: FoundryMessage


class ApprovalResponse(_InboundBase):
    kind: Literal["approval_response"] = "approval_response"
    approval_id: str
    decision: Literal["approved", "rejected"]
    reason: str | None = None


class CancelRun(_InboundBase):
    kind: Literal["cancel"] = "cancel"
    reason: str = "user_abort"


class PauseRun(_InboundBase):
    kind: Literal["pause"] = "pause"


class ResumeRun(_InboundBase):
    kind: Literal["resume"] = "resume"


InboundMessage = Annotated[
    InjectInput | ApprovalResponse | CancelRun | PauseRun | ResumeRun,
    Field(discriminator="kind"),
]


__all__ = [
    "AgentCompleted",
    "AgentStarted",
    "ApprovalRequiredEvent",
    "ApprovalResolved",
    "ApprovalResponse",
    "CancelRun",
    "ConnectionEvent",
    "EmbedCall",
    "ForgeIterationCompleted",
    "ForgeIterationStarted",
    "ForgeRollback",
    "ForgeStarted",
    "ForgeTerminated",
    "FunctionNodeCompleted",
    "FunctionNodeStarted",
    "Handoff",
    "InboundMessage",
    "InjectInput",
    "LLMCallCompleted",
    "LLMCallStarted",
    "LLMDelta",
    "MemoryConsolidate",
    "MemoryRead",
    "MemoryWriteEvent",
    "MetaAgentViolation",
    "PauseRun",
    "RerankEvent",
    "ResumeRun",
    "RetrievalEvent",
    "RunCancelledEvent",
    "RunCompleted",
    "RunEvent",
    "RunFailed",
    "RunStarted",
    "SemanticCacheHitEvent",
    "SemanticCacheInvalidate",
    "SemanticCacheMiss",
    "SemanticCacheStore",
    "StateTransition",
    "ToolCacheHit",
    "ToolCacheMiss",
    "ToolCacheStore",
    "ToolCompleted",
    "ToolStarted",
    "WarningEvent",
]
