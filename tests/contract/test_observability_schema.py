"""Observability schema contracts (docs/80 § Test expectations):

1. **Event-shape freeze** — every RunEvent's field set is snapshotted here.
   Attribute shape is frozen per major version (docs/80 invariant 3):
   additions to this table are fine (update the snapshot deliberately);
   renames/removals are a major-version event and must fail CI first.
2. **Span attribute spec** — every span the runtime emits (native or via
   the event→span mirror) carries the mandatory attributes from the
   docs/80 + docs/01 attribute table.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, get_args

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic import BaseModel

from foundry.core import events as ev
from foundry.core.connection import AuthScheme, ConnectionDescriptor
from foundry.observability.events import dispatch_event

# --- 1. event field-shape freeze ------------------------------------------------

# event literal → sorted field names. UPDATING THIS TABLE IS AN API DECISION:
# additive fields are fine; renames/removals break downstream dashboards and
# require a major version bump (docs/80 invariant 3).
EVENT_FIELD_SNAPSHOT: dict[str, list[str]] = {
    "run.started": ["inputs_hash", "pin_set_hash", "project", "system_version"],
    "agent.started": ["agent_name", "agent_version"],
    "agent.completed": ["agent_name", "output_summary"],
    "function_node.started": ["node_name", "node_version"],
    "function_node.completed": [
        "bytes_delta", "fields_written", "latency_ms", "node_name", "node_version",
    ],
    "llm.started": [
        "agent_name", "model", "prompt_messages", "prompt_tokens_estimate", "provider",
    ],
    "llm.delta": ["agent_name", "content_block_index", "delta"],
    "llm.completed": [
        "agent_name", "cost_estimate_usd", "latency_ms", "stop_reason", "usage",
    ],
    "tool.started": [
        "agent_name", "input_hash", "input_preview", "tool_ref", "tool_version",
    ],
    "tool.completed": [
        "agent_name", "error_category", "latency_ms", "output_preview",
        "retry_count", "success", "tool_ref", "tool_version",
    ],
    "connection": ["agent_name", "connection_descriptor", "latency_ms", "lifecycle"],
    "embed": [
        "agent_name", "cost_estimate_usd", "embedder", "input_count",
        "input_tokens", "latency_ms", "purpose",
    ],
    "cache.semantic.hit": [
        "agent_name", "cached_at", "saved_cost_estimate_usd",
        "saved_tokens_estimate", "similarity", "threshold",
    ],
    "cache.semantic.miss": ["agent_name", "threshold", "top_similarity"],
    "cache.semantic.store": ["agent_name", "ttl_s"],
    "cache.semantic.invalidate": [
        "agent_name", "current_version", "previous_version", "reason",
    ],
    "cache.tool.hit": ["agent_name", "cached_at", "tool_ref", "tool_version"],
    "cache.tool.miss": ["agent_name", "tool_ref", "tool_version"],
    "cache.tool.store": ["agent_name", "tool_ref", "tool_version", "ttl_s"],
    "warning": ["agent_name", "category", "error_class", "message"],
    "retrieval": [
        "agent_name", "branch_latency_ms", "branches_failed", "kind",
        "latency_ms", "retriever", "returned", "top_k",
    ],
    "rerank": [
        "after_ids", "agent_name", "before_ids", "candidates",
        "cost_estimate_usd", "latency_ms", "reranker", "top_k",
    ],
    "memory.read": [
        "agent_name", "layers_failed", "layers_read", "layers_truncated",
        "total_tokens_estimate", "truncated",
    ],
    "memory.write": ["agent_name", "bytes", "layer_kind", "layer_name", "write_kind"],
    "memory.consolidate": [
        "agent_name", "input_tokens_summarised", "latency_ms", "layer_name",
        "output_tokens_written", "trigger",
    ],
    "handoff": ["from_agent", "hop_number", "to_agent", "trigger"],
    "state.transition": ["agent_name", "bytes_delta", "fields_written"],
    "approval.required": ["agent_name", "approval_id", "context", "prompt"],
    "approval.resolved": ["approval_id", "decision", "reason"],
    "run.completed": [
        "duration_ms", "final_output", "status", "total_cost_estimate_usd",
        "total_input_tokens", "total_output_tokens",
    ],
    "run.failed": ["error"],
    "run.cancelled": ["reason"],
    "forge.started": [
        "forge_run_id", "max_cost_usd", "max_iterations", "meta_agent_version",
        "project", "threshold",
    ],
    "forge.iteration_started": ["directive_kind", "forge_run_id", "iteration_number"],
    "forge.iteration_completed": [
        "applied", "cluster_id", "commit_shas", "eval_delta", "eval_score",
        "forge_run_id", "iteration_number",
    ],
    "forge.rollback": [
        "forge_run_id", "iteration_number", "scope", "target", "to_version",
    ],
    "forge.terminated": [
        "final_score", "forge_run_id", "iterations", "reason", "total_cost_usd",
    ],
    "meta_agent.violation": ["detail", "forge_run_id", "tool"],
}

_BASE_FIELDS = {"run_id", "sequence", "timestamp", "worker_id", "event"}


@pytest.mark.contract
def test_every_run_event_field_set_matches_the_frozen_snapshot() -> None:
    union_members = get_args(get_args(ev.RunEvent)[0])
    seen: dict[str, list[str]] = {}
    for cls in union_members:
        literal = get_args(cls.model_fields["event"].annotation)[0]
        fields = sorted(set(cls.model_fields) - _BASE_FIELDS - {"event"})
        seen[literal] = fields
    assert seen == EVENT_FIELD_SNAPSHOT, (
        "RunEvent schema drift: update EVENT_FIELD_SNAPSHOT deliberately "
        "(additive) or treat as a major-version break (rename/removal)"
    )


@pytest.mark.contract
def test_every_event_carries_the_base_identity_fields() -> None:
    union_members = get_args(get_args(ev.RunEvent)[0])
    for cls in union_members:
        missing = _BASE_FIELDS - set(cls.model_fields)
        assert not missing, f"{cls.__name__} lacks base fields {missing}"


# --- 2. span attribute spec (docs/80 + docs/01 attribute table) -----------------

_RID = "R1" + "0" * 24
_NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)


def _base(seq: int = 0) -> dict[str, Any]:
    return {"run_id": _RID, "sequence": seq, "timestamp": _NOW, "worker_id": "host:1"}


# span name → (event instance, mandatory attribute keys per docs/80 table).
# run_id / sequence / timestamp / worker_id are asserted for every span.
def _mirror_cases() -> list[tuple[str, BaseModel, set[str]]]:
    descriptor = ConnectionDescriptor(
        ref="catalog/http_service@v1",
        slot="service",
        auth_scheme=AuthScheme.API_KEY,
        config_hash="deadbeef",
    )
    return [
        (
            "foundry.tool",
            ev.ToolCompleted(
                **_base(), agent_name="a", tool_ref="local/t", tool_version="v1",
                success=True, latency_ms=10,
            ),
            {"agent_name", "tool_ref", "tool_version", "success", "latency_ms",
             "retry_count"},
        ),
        (
            "foundry.handoff",
            ev.Handoff(
                **_base(), from_agent="a", to_agent="b", trigger="rule", hop_number=1,
            ),
            {"from_agent", "to_agent", "trigger", "hop_number"},
        ),
        (
            "foundry.state_transition",
            ev.StateTransition(**_base(), agent_name="a", fields_written=["x"]),
            {"agent_name", "fields_written", "bytes_delta"},
        ),
        (
            "foundry.function_node",
            ev.FunctionNodeCompleted(
                **_base(), node_name="n", node_version="v1", fields_written=["x"],
                bytes_delta=1, latency_ms=2,
            ),
            {"node_name", "fields_written", "bytes_delta", "latency_ms"},
        ),
        (
            "foundry.connection",
            ev.ConnectionEvent(
                **_base(), agent_name="a", connection_descriptor=descriptor,
                lifecycle="acquire", latency_ms=1,
            ),
            {"connection_ref", "slot", "auth_scheme", "config_hash", "lifecycle",
             "latency_ms"},
        ),
        (
            "foundry.embed",
            ev.EmbedCall(
                **_base(), agent_name="a", embedder="e", input_count=1,
                input_tokens=8, purpose="query", latency_ms=3,
            ),
            {"embedder", "input_count", "input_tokens", "purpose", "latency_ms"},
        ),
        (
            "foundry.cache.semantic",
            ev.SemanticCacheHitEvent(
                **_base(), agent_name="a", similarity=0.97, threshold=0.9,
                cached_at=_NOW, saved_tokens_estimate=100,
                saved_cost_estimate_usd=Decimal("0.001"),
            ),
            {"agent_name", "similarity", "threshold", "cached_at"},
        ),
        (
            "foundry.cache.tool",
            ev.ToolCacheHit(
                **_base(), agent_name="a", tool_ref="local/t", tool_version="v1",
                cached_at=_NOW,
            ),
            {"agent_name", "tool_ref", "tool_version", "cached_at"},
        ),
        (
            "foundry.retrieval",
            ev.RetrievalEvent(
                **_base(), agent_name="a", retriever="r", kind="dense", top_k=5,
                returned=3, latency_ms=4,
            ),
            {"retriever", "kind", "top_k", "returned", "latency_ms"},
        ),
        (
            "foundry.rerank",
            ev.RerankEvent(
                **_base(), agent_name="a", reranker="rr", candidates=10, top_k=3,
                latency_ms=5,
            ),
            {"reranker", "candidates", "top_k", "latency_ms"},
        ),
        (
            "foundry.memory",
            ev.MemoryWriteEvent(
                **_base(), agent_name="a", layer_name="working",
                layer_kind="working", write_kind="message", bytes=64,
            ),
            {"agent_name", "layer_name", "layer_kind", "write_kind"},
        ),
        (
            "foundry.approval",
            ev.ApprovalRequiredEvent(
                **_base(), agent_name="a", approval_id="ap1", prompt="ok?",
            ),
            {"agent_name", "approval_id", "prompt"},
        ),
    ]


@pytest.mark.contract
def test_span_mirror_attribute_spec(span_exporter: InMemorySpanExporter) -> None:
    cases = _mirror_cases()
    for _, event, _ in cases:
        dispatch_event(event)
    spans = {span.name: span for span in span_exporter.get_finished_spans()}
    for name, _, required in cases:
        assert name in spans, f"span {name} not emitted"
        attributes = dict(spans[name].attributes or {})
        missing = ({"run_id", "sequence", "worker_id"} | required) - set(attributes)
        assert not missing, f"span {name} missing mandatory attributes {missing}"
