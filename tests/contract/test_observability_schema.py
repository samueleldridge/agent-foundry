"""Observability schema contracts (docs/80 § Test expectations):

1. **Event-shape freeze** — every RunEvent's field set matches the frozen
   attribute contract. Attribute shape is frozen per major version
   (docs/80 invariant 3): additions are fine (update the contract file
   deliberately); renames/removals are a major-version event and must
   fail CI first.
2. **Span attribute spec** — every span the runtime emits via the
   event→span mirror carries the mandatory attributes from the contract.

The contract itself is NOT hardcoded here: it lives in
``docs/80-observability-attributes.yaml`` (the machine-readable extract of
the docs/80 attribute tables — Phase 9 review follow-up 4), so the doc and
this test can no longer drift apart silently. The doc references the YAML;
this test parses it at test time and compares it against the code.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, get_args

import pytest
import yaml
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic import BaseModel

from foundry.core import events as ev
from foundry.core.connection import AuthScheme, ConnectionDescriptor
from foundry.observability.events import dispatch_event

# --- the machine-readable contract (docs/80-observability-attributes.yaml) ------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONTRACT_PATH = _REPO_ROOT / "docs" / "80-observability-attributes.yaml"


def _load_contract() -> dict[str, Any]:
    loaded = yaml.safe_load(_CONTRACT_PATH.read_text())
    assert isinstance(loaded, dict), f"{_CONTRACT_PATH} must parse to a mapping"
    for key in ("base_event_fields", "events", "span_base_attributes", "spans"):
        assert key in loaded, f"{_CONTRACT_PATH} lacks the {key!r} section"
    return loaded


_CONTRACT = _load_contract()
_BASE_FIELDS: set[str] = set(_CONTRACT["base_event_fields"])
EVENT_FIELD_CONTRACT: dict[str, list[str]] = {
    literal: sorted(fields) for literal, fields in _CONTRACT["events"].items()
}
_SPAN_BASE_ATTRS: set[str] = set(_CONTRACT["span_base_attributes"])
SPAN_ATTR_CONTRACT: dict[str, set[str]] = {
    name: set(attrs) for name, attrs in _CONTRACT["spans"].items()
}


# --- 1. event field-shape freeze ------------------------------------------------


@pytest.mark.contract
def test_every_run_event_field_set_matches_the_frozen_contract() -> None:
    union_members = get_args(get_args(ev.RunEvent)[0])
    seen: dict[str, list[str]] = {}
    for cls in union_members:
        literal = get_args(cls.model_fields["event"].annotation)[0]
        fields = sorted(set(cls.model_fields) - _BASE_FIELDS)
        seen[literal] = fields
    assert seen == EVENT_FIELD_CONTRACT, (
        "RunEvent schema drift against docs/80-observability-attributes.yaml: "
        "update the contract file deliberately (additive) or treat as a "
        "major-version break (rename/removal)"
    )


@pytest.mark.contract
def test_every_event_carries_the_base_identity_fields() -> None:
    union_members = get_args(get_args(ev.RunEvent)[0])
    for cls in union_members:
        missing = _BASE_FIELDS - set(cls.model_fields)
        assert not missing, f"{cls.__name__} lacks base fields {missing}"


# --- 2. span attribute spec ------------------------------------------------------

_RID = "R1" + "0" * 24
_NOW = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)


def _base(seq: int = 0) -> dict[str, Any]:
    return {"run_id": _RID, "sequence": seq, "timestamp": _NOW, "worker_id": "host:1"}


# span name → a representative event instance; the mandatory attribute set
# comes from the contract file, NOT from this table.
def _mirror_cases() -> dict[str, BaseModel]:
    descriptor = ConnectionDescriptor(
        ref="catalog/http_service@v1",
        slot="service",
        auth_scheme=AuthScheme.API_KEY,
        config_hash="deadbeef",
    )
    return {
        "foundry.tool": ev.ToolCompleted(
            **_base(), agent_name="a", tool_ref="local/t", tool_version="v1",
            success=True, latency_ms=10,
        ),
        "foundry.handoff": ev.Handoff(
            **_base(), from_agent="a", to_agent="b", trigger="rule", hop_number=1,
        ),
        "foundry.state_transition": ev.StateTransition(
            **_base(), agent_name="a", fields_written=["x"],
        ),
        "foundry.function_node": ev.FunctionNodeCompleted(
            **_base(), node_name="n", node_version="v1", fields_written=["x"],
            bytes_delta=1, latency_ms=2,
        ),
        "foundry.connection": ev.ConnectionEvent(
            **_base(), agent_name="a", connection_descriptor=descriptor,
            lifecycle="acquire", latency_ms=1,
        ),
        "foundry.embed": ev.EmbedCall(
            **_base(), agent_name="a", embedder="e", input_count=1,
            input_tokens=8, purpose="query", latency_ms=3,
        ),
        "foundry.cache.semantic": ev.SemanticCacheHitEvent(
            **_base(), agent_name="a", similarity=0.97, threshold=0.9,
            cached_at=_NOW, saved_tokens_estimate=100,
            saved_cost_estimate_usd=Decimal("0.001"),
        ),
        "foundry.cache.tool": ev.ToolCacheHit(
            **_base(), agent_name="a", tool_ref="local/t", tool_version="v1",
            cached_at=_NOW,
        ),
        "foundry.retrieval": ev.RetrievalEvent(
            **_base(), agent_name="a", retriever="r", kind="dense", top_k=5,
            returned=3, latency_ms=4,
        ),
        "foundry.rerank": ev.RerankEvent(
            **_base(), agent_name="a", reranker="rr", candidates=10, top_k=3,
            latency_ms=5,
        ),
        "foundry.memory": ev.MemoryWriteEvent(
            **_base(), agent_name="a", layer_name="working",
            layer_kind="working", write_kind="message", bytes=64,
        ),
        "foundry.approval": ev.ApprovalRequiredEvent(
            **_base(), agent_name="a", approval_id="ap1", prompt="ok?",
        ),
    }


@pytest.mark.contract
def test_every_contract_span_has_a_probe_case() -> None:
    """The contract file and this test's probe events cover the same span
    set — an addition to either side without the other fails here."""
    assert set(_mirror_cases()) == set(SPAN_ATTR_CONTRACT), (
        "span set drift between docs/80-observability-attributes.yaml and "
        "the probe events in this test"
    )


@pytest.mark.contract
def test_span_mirror_attribute_spec(span_exporter: InMemorySpanExporter) -> None:
    cases = _mirror_cases()
    for event in cases.values():
        dispatch_event(event)
    spans = {span.name: span for span in span_exporter.get_finished_spans()}
    for name, required in SPAN_ATTR_CONTRACT.items():
        assert name in spans, f"span {name} not emitted"
        attributes = dict(spans[name].attributes or {})
        missing = (_SPAN_BASE_ATTRS | required) - set(attributes)
        assert not missing, (
            f"span {name} missing mandatory attributes {missing} (contract: "
            "docs/80-observability-attributes.yaml)"
        )
