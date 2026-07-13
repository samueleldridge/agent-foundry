"""Unit tests: dispatcher fan-out, degradation guard, span mirror,
attribute redaction (docs/80)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from pydantic import BaseModel

from foundry.core.connection import AuthScheme, ConnectionDescriptor
from foundry.core.events import (
    ConnectionEvent,
    Handoff,
    RetrievalEvent,
    StateTransition,
    ToolCompleted,
)
from foundry.observability.events import (
    ObservabilityDispatcher,
    dispatch_event,
    event_attributes,
)
from foundry.observability.redaction import redact_attributes, truncate_preview

_NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


def _base(seq: int = 0) -> dict[str, object]:
    return {
        "run_id": "R1" + "0" * 24,
        "sequence": seq,
        "timestamp": _NOW,
        "worker_id": "host:1",
    }


@pytest.mark.unit
def test_dispatcher_guards_handler_failures() -> None:
    dispatcher = ObservabilityDispatcher()
    seen: list[str] = []

    def bad(_: BaseModel) -> None:
        raise RuntimeError("exporter down")

    def good(event: BaseModel) -> None:
        seen.append(type(event).__name__)

    dispatcher.add_handler("bad", bad)
    dispatcher.add_handler("good", good)
    event = StateTransition(**_base(), agent_name="a", fields_written=["x"])  # type: ignore[arg-type]
    dispatcher.dispatch(event)
    dispatcher.dispatch(event)
    assert seen == ["StateTransition", "StateTransition"]


@pytest.mark.unit
def test_span_mirror_emits_subsystem_spans(
    span_exporter: InMemorySpanExporter,
) -> None:
    dispatch_event(
        ToolCompleted(
            **_base(),  # type: ignore[arg-type]
            agent_name="hello_agent",
            tool_ref="catalog/http_get_json",
            tool_version="v1",
            success=True,
            latency_ms=40,
        )
    )
    dispatch_event(
        Handoff(
            **_base(1),  # type: ignore[arg-type]
            from_agent="a",
            to_agent="b",
            trigger="rule",
            hop_number=2,
        )
    )
    dispatch_event(
        StateTransition(**_base(2), agent_name="a", fields_written=["x"])  # type: ignore[arg-type]
    )
    spans = {span.name: span for span in span_exporter.get_finished_spans()}
    assert set(spans) == {"foundry.tool", "foundry.handoff", "foundry.state_transition"}

    tool_span = spans["foundry.tool"]
    assert tool_span.attributes is not None
    assert tool_span.attributes["run_id"] == "R1" + "0" * 24
    assert tool_span.attributes["tool_ref"] == "catalog/http_get_json"
    assert tool_span.attributes["success"] is True
    # latency back-computation: span duration == latency_ms
    assert tool_span.start_time is not None and tool_span.end_time is not None
    assert tool_span.end_time - tool_span.start_time == 40 * 1_000_000

    handoff_span = spans["foundry.handoff"]
    assert handoff_span.attributes is not None
    assert handoff_span.attributes["from_agent"] == "a"
    assert handoff_span.attributes["hop_number"] == 2


@pytest.mark.unit
def test_connection_event_attributes_use_redacted_descriptor() -> None:
    event = ConnectionEvent(
        **_base(),  # type: ignore[arg-type]
        agent_name="a",
        connection_descriptor=ConnectionDescriptor(
            ref="catalog/http_service@v1",
            slot="service",
            auth_scheme=AuthScheme.API_KEY,
            config_hash="deadbeef",
            principal="svc-account",
            redacted_config={"base_url": "https://example.test"},
        ),
        lifecycle="acquire",
        latency_ms=5,
    )
    attributes = event_attributes(event)
    assert attributes["connection_ref"] == "catalog/http_service@v1"
    assert attributes["slot"] == "service"
    assert attributes["auth_scheme"] == "api_key"
    assert attributes["principal"] == "svc-account"
    # the raw descriptor object must not leak wholesale
    assert "connection_descriptor" not in attributes


@pytest.mark.unit
def test_event_attributes_flatten_lists_and_redact() -> None:
    event = RetrievalEvent(
        **_base(),  # type: ignore[arg-type]
        agent_name="a",
        retriever="docs",
        kind="hybrid",
        top_k=5,
        returned=3,
        latency_ms=12,
        branch_latency_ms={"dense": 8, "sparse": 9},
        branches_failed=["sparse"],
    )
    attributes = event_attributes(event)
    assert attributes["branches_failed"] == "sparse"
    assert attributes["branch_latency_ms.dense"] == 8


@pytest.mark.unit
def test_redact_attributes_drops_denylisted_keys_and_secret_values() -> None:
    out = redact_attributes(
        {
            "api_key": "abc12345",
            "nested": {"password": "x", "ok": 1},
            "leaky": "sk-ant-abcdefgh12345678",
            "aws": "AKIAABCDEFGHIJKLMNOP",
            "fine": "hello",
        }
    )
    assert out == {"nested": {"ok": 1}, "fine": "hello"}


@pytest.mark.unit
def test_preview_truncation() -> None:
    long_text = "x" * 900
    out = redact_attributes({"input_preview": long_text})
    assert len(out["input_preview"]) < 600
    assert out["input_preview"].endswith("[truncated]")
    assert truncate_preview("short") == "short"


@pytest.mark.unit
def test_llm_completed_cost_decimal_survives_as_attribute_string() -> None:
    from foundry.core.events import LLMCallCompleted
    from foundry.core.model import StopReason, TokenUsage

    event = LLMCallCompleted(
        **_base(),  # type: ignore[arg-type]
        agent_name="a",
        usage=TokenUsage(input_tokens=1, output_tokens=2),
        cost_estimate_usd=Decimal("0.005"),
        latency_ms=10,
        stop_reason=StopReason.END_TURN,
    )
    attributes = event_attributes(event)
    assert attributes["cost_estimate_usd"] == "0.005"
    assert attributes["usage.input_tokens"] == 1
