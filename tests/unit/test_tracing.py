"""Span helpers (docs/01 § Observability event spec, Phase 3 slice)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from foundry.observability.tracing import (
    foundry_span,
    set_span_attributes,
    worker_id,
)


@pytest.mark.unit
def test_span_attributes_cleaned_and_recorded(span_exporter: Any) -> None:
    with foundry_span(
        "foundry.llm",
        {
            "run_id": "01ABC",
            "latency_ms": 12,
            "cost_estimate_usd": Decimal("0.0015"),  # coerced to str
            "error": None,  # dropped
        },
    ) as span:
        set_span_attributes(span, {"stop_reason": "end_turn", "skip": None})

    spans = span_exporter.get_finished_spans()
    assert [s.name for s in spans] == ["foundry.llm"]
    attributes = dict(spans[0].attributes or {})
    assert attributes["run_id"] == "01ABC"
    assert attributes["latency_ms"] == 12
    assert attributes["cost_estimate_usd"] == "0.0015"
    assert attributes["stop_reason"] == "end_turn"
    assert "error" not in attributes
    assert "skip" not in attributes


@pytest.mark.unit
def test_span_records_exception_and_reraises(span_exporter: Any) -> None:
    with pytest.raises(ValueError, match="boom"):
        with foundry_span("foundry.node", {"node": "n"}):
            raise ValueError("boom")
    spans = span_exporter.get_finished_spans()
    assert len(spans) == 1
    assert not spans[0].status.is_ok
    assert spans[0].events[0].name == "exception"


@pytest.mark.unit
def test_nested_spans_parent_correctly(span_exporter: Any) -> None:
    with foundry_span("foundry.run", {"run_id": "r"}) as run_span:
        with foundry_span("foundry.node", {"node": "agent"}):
            pass
    spans = {s.name: s for s in span_exporter.get_finished_spans()}
    node_parent = spans["foundry.node"].parent
    assert node_parent is not None
    assert node_parent.span_id == run_span.get_span_context().span_id


@pytest.mark.unit
def test_worker_id_is_host_and_pid() -> None:
    host, _, pid = worker_id().partition(":")
    assert host and pid.isdigit()
