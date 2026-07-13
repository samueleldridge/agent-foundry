"""Unit tests for the SQLite event-mirror (docs/80 § transport 3)."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from foundry.core.errors import ConfigError
from foundry.core.events import (
    Handoff,
    LLMCallCompleted,
    LLMCallStarted,
    RunCompleted,
    RunFailed,
    RunStarted,
    ToolCompleted,
)
from foundry.core.model import StopReason, TokenUsage
from foundry.observability.store import ObservabilityStore, parse_since

_NOW = datetime(2026, 7, 10, 12, 0, 0, tzinfo=UTC)

def _rid(name: str) -> str:
    """Deterministic 26-char Crockford ULID-shaped id for tests."""
    base = re.sub(r"[^0-9A-HJKMNP-TV-Z]", "0", name.upper())
    return (base + "0" * 26)[:26]



def _feed_run(
    store: ObservabilityStore,
    *,
    run_id: str,
    project: str = "hello",
    model: str = "claude-haiku-4-5",
    cost: str = "0.001",
    latency_ms: int = 120,
    tool_success: bool = True,
    when: datetime = _NOW,
) -> None:
    seq = 0

    def base() -> dict[str, object]:
        nonlocal seq
        seq += 1
        return {
            "run_id": _rid(run_id),
            "sequence": seq,
            "timestamp": when,
            "worker_id": "host:1",
        }

    store.record_event(
        RunStarted(
            **base(),  # type: ignore[arg-type]
            project=project,
            system_version="abc123",
            pin_set_hash="p1",
            inputs_hash="i1",
        )
    )
    store.record_event(
        LLMCallStarted(
            **base(),  # type: ignore[arg-type]
            agent_name="hello_agent",
            provider="anthropic",
            model=model,
        )
    )
    store.record_event(
        LLMCallCompleted(
            **base(),  # type: ignore[arg-type]
            agent_name="hello_agent",
            usage=TokenUsage(input_tokens=50, output_tokens=20),
            cost_estimate_usd=Decimal(cost),
            latency_ms=latency_ms,
            stop_reason=StopReason.END_TURN,
        )
    )
    store.record_event(
        ToolCompleted(
            **base(),  # type: ignore[arg-type]
            agent_name="hello_agent",
            tool_ref="catalog/http_get_json",
            tool_version="v1",
            success=tool_success,
            latency_ms=30,
            error_category=None if tool_success else "timeout",
        )
    )
    store.record_event(
        Handoff(
            **base(),  # type: ignore[arg-type]
            from_agent="hello_agent",
            to_agent="__end__",
            trigger="end",
            hop_number=1,
        )
    )
    store.record_event(
        RunCompleted(
            **base(),  # type: ignore[arg-type]
            total_input_tokens=50,
            total_output_tokens=20,
            total_cost_estimate_usd=Decimal(cost),
            duration_ms=500,
        )
    )


@pytest.fixture
def store(tmp_path: Path) -> ObservabilityStore:
    return ObservabilityStore(tmp_path / "observability.db")


@pytest.mark.unit
def test_run_lifecycle_rows(store: ObservabilityStore) -> None:
    _feed_run(store, run_id="r1")
    runs = store.recent_runs()
    assert len(runs) == 1
    row = runs[0]
    assert row["run_id"] == _rid("r1")
    assert row["project"] == "hello"
    assert row["status"] == "success"
    assert row["total_cost_usd"] == pytest.approx(0.001)
    assert row["duration_ms"] == 500


@pytest.mark.unit
def test_llm_call_row_carries_provider_model_from_started(
    store: ObservabilityStore,
) -> None:
    _feed_run(store, run_id="r1")
    rows = store.cost_breakdown(by="model")
    assert rows == [
        {
            "bucket": "anthropic:claude-haiku-4-5",
            "calls": 1,
            "input_tokens": 50,
            "output_tokens": 20,
            "cost_usd": pytest.approx(0.001),
        }
    ]


@pytest.mark.unit
def test_cost_breakdown_filters_project_and_since(store: ObservabilityStore) -> None:
    old = _NOW - timedelta(days=10)
    _feed_run(store, run_id="r_old", when=old, cost="0.5")
    _feed_run(store, run_id="r_new", cost="0.25")
    _feed_run(store, run_id="r_other", project="other", cost="0.125")

    assert store.total_cost() == pytest.approx(0.875)
    assert store.total_cost(project="hello") == pytest.approx(0.75)
    cutoff = _NOW - timedelta(days=7)
    assert store.total_cost(project="hello", since=cutoff) == pytest.approx(0.25)


@pytest.mark.unit
def test_cost_breakdown_by_day_and_agent(store: ObservabilityStore) -> None:
    _feed_run(store, run_id="r1")
    _feed_run(store, run_id="r2", when=_NOW - timedelta(days=1))
    by_day = store.cost_breakdown(by="day")
    assert {row["bucket"] for row in by_day} == {"2026-07-10", "2026-07-09"}
    by_agent = store.cost_breakdown(by="agent")
    assert by_agent[0]["bucket"] == "hello_agent"
    with pytest.raises(ConfigError):
        store.cost_breakdown(by="nope")


@pytest.mark.unit
def test_tool_failures_aggregation(store: ObservabilityStore) -> None:
    _feed_run(store, run_id="r1", tool_success=True)
    _feed_run(store, run_id="r2", tool_success=False)
    rows = store.tool_failures()
    assert len(rows) == 1
    row = rows[0]
    assert row["tool_ref"] == "catalog/http_get_json"
    assert row["calls"] == 2
    assert row["failures"] == 1
    assert row["failure_rate"] == pytest.approx(0.5)
    assert row["last_error_category"] == "timeout"


@pytest.mark.unit
def test_latency_percentiles(store: ObservabilityStore) -> None:
    for index in range(1, 21):
        _feed_run(store, run_id=f"r{index}", latency_ms=index * 10)
    rows = store.latency_percentiles()
    assert len(rows) == 1
    row = rows[0]
    assert row["calls"] == 20
    assert row["p50_ms"] == 100
    assert row["p95_ms"] == 190


@pytest.mark.unit
def test_run_failed_records_error_class(store: ObservabilityStore) -> None:
    store.record_event(
        RunStarted(
            run_id=_rid("rf"),
            sequence=0,
            timestamp=_NOW,
            worker_id="host:1",
            project="hello",
            system_version="abc",
            pin_set_hash="p",
            inputs_hash="i",
        )
    )
    store.record_event(
        RunFailed(
            run_id=_rid("rf"),
            sequence=1,
            timestamp=_NOW,
            worker_id="host:1",
            error={"error": "ProviderTimeoutError", "message": "boom"},
        )
    )
    row = store.recent_runs()[0]
    assert row["status"] == "failed"


@pytest.mark.unit
def test_record_eval_round_trip(store: ObservabilityStore) -> None:
    store.record_eval(
        eval_run_id="e1",
        project="hello",
        eval_name="greeting",
        target_ref="project:hello",
        target_version="abc",
        eval_spec_hash="h1",
        score=0.9,
        threshold=0.8,
        passed=True,
        cases_total=5,
        cases_passed=5,
        cost_total_usd=0.01,
        completed_at=_NOW.isoformat(),
    )
    rows = store.eval_rows(project="hello")
    assert rows[0]["score"] == pytest.approx(0.9)
    assert rows[0]["passed"] == 1


@pytest.mark.unit
def test_schema_version_row(store: ObservabilityStore, tmp_path: Path) -> None:
    _feed_run(store, run_id="r1")
    import sqlite3

    conn = sqlite3.connect(tmp_path / "observability.db")
    assert conn.execute("SELECT schema_version FROM schema_meta").fetchone() == (1,)
    conn.close()


@pytest.mark.unit
def test_parse_since() -> None:
    now = _NOW
    assert parse_since("7d", now=now) == now - timedelta(days=7)
    assert parse_since("24h", now=now) == now - timedelta(hours=24)
    assert parse_since("30m", now=now) == now - timedelta(minutes=30)
    with pytest.raises(ConfigError):
        parse_since("7 days", now=now)
    with pytest.raises(ConfigError):
        parse_since("", now=now)
