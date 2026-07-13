"""Unit tests: the OTel metrics catalogue aggregates cleanly (docs/80
§ transport 2 — 'compute total cost for project X last 7 days from the
metric stream')."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from foundry.core.events import (
    Handoff,
    LLMCallCompleted,
    LLMCallStarted,
    RunCompleted,
    RunStarted,
    ToolCompleted,
)
from foundry.core.model import StopReason, TokenUsage
from foundry.observability.events import dispatch_event

_NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)


def _rid() -> str:
    return uuid.uuid4().hex.upper().replace("I", "0").replace("L", "0").replace(
        "O", "0"
    ).replace("U", "0")[:26].ljust(26, "0")


def _drive_run(*, project: str, cost: str, calls: int = 1) -> None:
    run_id = _rid()
    seq = 0

    def base() -> dict[str, object]:
        nonlocal seq
        seq += 1
        return {
            "run_id": run_id,
            "sequence": seq,
            "timestamp": _NOW,
            "worker_id": "host:1",
        }

    dispatch_event(
        RunStarted(
            **base(),  # type: ignore[arg-type]
            project=project,
            system_version="v",
            pin_set_hash="p",
            inputs_hash="i",
        )
    )
    for _ in range(calls):
        dispatch_event(
            LLMCallStarted(
                **base(),  # type: ignore[arg-type]
                agent_name="agent_a",
                provider="anthropic",
                model="claude-haiku-4-5",
            )
        )
        dispatch_event(
            LLMCallCompleted(
                **base(),  # type: ignore[arg-type]
                agent_name="agent_a",
                usage=TokenUsage(input_tokens=100, output_tokens=10),
                cost_estimate_usd=Decimal(cost),
                latency_ms=50,
                stop_reason=StopReason.END_TURN,
            )
        )
    dispatch_event(
        ToolCompleted(
            **base(),  # type: ignore[arg-type]
            agent_name="agent_a",
            tool_ref="local/t",
            tool_version="v1",
            success=True,
            latency_ms=5,
        )
    )
    dispatch_event(
        Handoff(
            **base(),  # type: ignore[arg-type]
            from_agent="agent_a",
            to_agent="__end__",
            trigger="end",
            hop_number=1,
        )
    )
    dispatch_event(
        RunCompleted(
            **base(),  # type: ignore[arg-type]
            total_input_tokens=100 * calls,
            total_output_tokens=10 * calls,
            total_cost_estimate_usd=Decimal(cost) * calls,
            duration_ms=200,
        )
    )


@pytest.mark.unit
def test_seven_day_project_cost_is_computable_from_the_metric_stream(
    metric_reader: InMemoryMetricReader,
    read_metric_points_fn: Callable[..., list[tuple[dict[str, object], float]]],
) -> None:
    read_metric_points = read_metric_points_fn
    """The docs/03 exit-gate aggregation: sum foundry.llm.cost_usd data
    points for one project across many runs ('7 days' of traffic); the
    project dimension isolates it from other projects' spend."""
    project = f"proj_{uuid.uuid4().hex[:8]}"
    other = f"proj_{uuid.uuid4().hex[:8]}"
    for _ in range(7):  # one run per 'day'
        _drive_run(project=project, cost="0.002")
    _drive_run(project=other, cost="0.5")

    points = read_metric_points(metric_reader, "foundry.llm.cost_usd")
    project_cost = sum(v for attrs, v in points if attrs.get("project") == project)
    assert project_cost == pytest.approx(0.014)

    run_cost_points = read_metric_points(metric_reader, "foundry.run.cost_usd")
    run_cost = sum(v for attrs, v in run_cost_points if attrs.get("project") == project)
    assert run_cost == pytest.approx(0.014)


@pytest.mark.unit
def test_llm_tool_handoff_and_run_instruments_are_tagged(
    metric_reader: InMemoryMetricReader,
    read_metric_points_fn: Callable[..., list[tuple[dict[str, object], float]]],
) -> None:
    read_metric_points = read_metric_points_fn
    project = f"proj_{uuid.uuid4().hex[:8]}"
    _drive_run(project=project, cost="0.001", calls=2)

    llm_calls = read_metric_points(metric_reader, "foundry.llm.calls_total")
    mine = [
        (attrs, v)
        for attrs, v in llm_calls
        if attrs.get("provider") == "anthropic" and attrs.get("agent") == "agent_a"
    ]
    assert sum(v for _, v in mine) >= 2
    assert all(attrs.get("model") == "claude-haiku-4-5" for attrs, _ in mine)

    tokens = read_metric_points(metric_reader, "foundry.run.input_tokens")
    project_tokens = sum(v for attrs, v in tokens if attrs.get("project") == project)
    assert project_tokens == 200

    tool_calls = read_metric_points(metric_reader, "foundry.tool.calls_total")
    assert any(
        attrs.get("tool_ref") == "local/t" and attrs.get("success") == "true"
        for attrs, _ in tool_calls
    )

    handoffs = read_metric_points(metric_reader, "foundry.handoff_total")
    assert any(
        attrs.get("from_agent") == "agent_a" and attrs.get("trigger") == "end"
        for attrs, _ in handoffs
    )

    runs = read_metric_points(metric_reader, "foundry.run.total")
    assert any(
        attrs.get("project") == project and attrs.get("status") == "success"
        for attrs, _ in runs
    )

    latency = read_metric_points(metric_reader, "foundry.llm.latency_ms")
    assert latency, "latency histogram should have data points"


@pytest.mark.unit
def test_eval_metrics_recorded_via_direct_api(
    metric_reader: InMemoryMetricReader,
    read_metric_points_fn: Callable[..., list[tuple[dict[str, object], float]]],
) -> None:
    read_metric_points = read_metric_points_fn
    from foundry.observability.metrics import get_metrics_recorder

    project = f"proj_{uuid.uuid4().hex[:8]}"
    get_metrics_recorder().record_eval(
        project=project, target_ref="project:x", eval_spec_hash="h", score=0.87
    )
    # NOTE: one collection only — a synchronous gauge reports its point in
    # the first collect after a measurement; counters are cumulative.
    scores = read_metric_points(metric_reader, "foundry.eval.score")
    mine = [v for attrs, v in scores if attrs.get("project") == project]
    assert mine == [pytest.approx(0.87)]
    runs = read_metric_points(metric_reader, "foundry.eval.runs_total")
    assert any(attrs.get("project") == project for attrs, _ in runs)
