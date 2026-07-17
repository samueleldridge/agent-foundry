"""Phase 9 observability exit gates (docs/03 § Phase 9):

- every run produces OTel traces with the mandatory attributes;
- the SQLite mirror captures the same events — `foundry obs` results
  match the event stream (the OTel-side source of truth);
- metrics aggregate cleanly per project;
- the RunArtifact is complete: metadata, inputs, outputs, state
  transitions, llm_calls + tool_calls JSONL.
"""

from __future__ import annotations

import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from foundry.cli.obs import execute_cost, execute_p95, execute_tool_failures
from foundry.cli.run import execute_run
from foundry.observability.events import get_store

REPO_ROOT = Path(__file__).resolve().parents[2]
HELLO_DIR = REPO_ROOT / "projects" / "hello"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


class Transport:
    def __init__(self, llm_turns: list[dict[str, Any]]) -> None:
        self.llm_turns = llm_turns
        self.llm_requests: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openai.com":
            self.llm_requests.append(json.loads(request.content))
            return httpx.Response(
                200, json=self.llm_turns[len(self.llm_requests) - 1]
            )
        if request.url.host == "worldtimeapi.org":
            return httpx.Response(
                200, json={"datetime": "2026-07-13T10:00:00+00:00"}
            )
        raise AssertionError(f"unexpected host: {request.url.host}")

    def build(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _tool_turns() -> list[dict[str, Any]]:
    return [
        {
            "model": "gpt-5-mini",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Checking.",
                        "tool_calls": [
                            {
                                "id": "tu_1",
                                "type": "function",
                                "function": {
                                    "name": "get_time",
                                    "arguments": json.dumps(
                                        {"path": "/api/timezone/Etc/UTC"}
                                    ),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 30},
        },
        {
            "model": "gpt-5-mini",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": json.dumps({"greeting": "Hello at 10:00!"}),
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 90, "completion_tokens": 25},
        },
    ]


def _events(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]


def _single_run_dir(tmp_path: Path) -> Path:
    dirs = sorted((tmp_path / "foundry_home" / "runs").iterdir())
    assert len(dirs) == 1
    return dirs[0]


@pytest.mark.integration
def test_traces_mirror_and_artifact_for_one_run(
    tmp_path: Path,
    span_exporter: InMemorySpanExporter,
    metric_reader: InMemoryMetricReader,
    capsys: pytest.CaptureFixture[str],
) -> None:
    transport = Transport(_tool_turns())
    code = execute_run(HELLO_DIR, '{"name": "world"}', transport=transport.build())
    assert code == 0
    capsys.readouterr()

    run_dir = _single_run_dir(tmp_path)
    events = _events(run_dir)
    kinds = {event["event"] for event in events}
    assert {
        "run.started", "agent.started", "llm.started", "llm.completed",
        "tool.started", "tool.completed", "connection", "run.completed",
    } <= kinds

    # --- 1. spans: native (run/node/llm) + mirrored (tool/connection) with
    # the mandatory identity attributes on every one.
    spans = span_exporter.get_finished_spans()
    by_name = {span.name for span in spans}
    assert {"foundry.run", "foundry.node", "foundry.llm", "foundry.tool",
            "foundry.connection"} <= by_name
    run_id = events[0]["run_id"]
    for span in spans:
        attributes = dict(span.attributes or {})
        if span.name.startswith("foundry."):
            assert attributes.get("run_id") == run_id, span.name

    # tool span carries the docs/80 tool attributes
    tool_span = next(s for s in spans if s.name == "foundry.tool")
    tool_attrs = dict(tool_span.attributes or {})
    assert tool_attrs["tool_ref"] == "catalog/http_get_json"
    assert tool_attrs["success"] is True

    # --- 2. SQLite mirror captures the same events; totals match the
    # event stream exactly (docs/80 invariant 4).
    store = get_store()
    llm_events = [e for e in events if e["event"] == "llm.completed"]
    expected_cost = sum(
        Decimal(str(e["cost_estimate_usd"])) for e in llm_events
        if e.get("cost_estimate_usd") is not None
    )
    assert store.total_cost(project="hello") == pytest.approx(
        float(expected_cost), rel=1e-6
    )
    breakdown = store.cost_breakdown(project="hello", by="model")
    assert len(breakdown) == 1
    assert breakdown[0]["calls"] == len(llm_events)
    assert breakdown[0]["input_tokens"] == sum(
        e["usage"]["input_tokens"] for e in llm_events
    )
    run_rows = store.recent_runs(project="hello")
    assert len(run_rows) == 1
    assert run_rows[0]["run_id"] == run_id
    assert run_rows[0]["status"] == "success"
    tool_rows = store.tool_failures(project="hello")
    assert tool_rows[0]["calls"] == 1
    assert tool_rows[0]["failures"] == 0

    # --- 3. foundry obs CLI output matches the store/stream.
    assert execute_cost(project="hello", since=None, by="model", json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["total_cost_usd"] == pytest.approx(float(expected_cost), rel=1e-6)
    assert payload["rows"][0]["calls"] == len(llm_events)

    assert execute_p95(model=None, project="hello", since=None, json_output=True) == 0
    p95_rows = json.loads(capsys.readouterr().out)
    assert p95_rows[0]["calls"] == len(llm_events)

    assert (
        execute_tool_failures(tool=None, project="hello", since=None, json_output=True)
        == 0
    )
    tf_rows = json.loads(capsys.readouterr().out)
    assert tf_rows[0]["tool_ref"] == "catalog/http_get_json"

    # --- 4. RunArtifact completeness (docs/81): every documented file.
    assert (run_dir / "metadata.json").exists()
    assert (run_dir / "events.jsonl").exists()
    assert (run_dir / "llm_calls.jsonl").exists()
    assert (run_dir / "tool_calls.jsonl").exists()
    inputs = json.loads((run_dir / "inputs.json").read_text())
    assert inputs == {"inputs": {"name": "world"}}
    outputs = json.loads((run_dir / "outputs.json").read_text())
    assert outputs["output"]["greeting"] == "Hello at 10:00!"
    metadata = json.loads((run_dir / "metadata.json").read_text())
    assert metadata["status"] == "completed"
    assert metadata["run_id"] == run_id

    # --- 5. state_transitions.jsonl content (docs/80 § flow control +
    # docs/81 artifact layout): one record per agent state mutation, with
    # the full identity + payload field set.
    transitions = [
        json.loads(line)
        for line in (run_dir / "state_transitions.jsonl").read_text().splitlines()
    ]
    assert len(transitions) == 1
    transition = transitions[0]
    assert transition["event"] == "state.transition"
    assert transition["run_id"] == run_id
    assert transition["agent_name"] == "hello_agent"
    assert transition["fields_written"] == ["greeting"]
    assert transition["bytes_delta"] > 0
    assert isinstance(transition["sequence"], int)
    assert transition["timestamp"]
    assert transition["worker_id"]
    # the same records appear (in order) in the full event stream
    stream_transitions = [e for e in events if e["event"] == "state.transition"]
    assert stream_transitions == transitions


@pytest.mark.integration
def test_capture_inputs_false_excludes_inputs_from_the_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """docs/80 § Redaction + docs/81: with ``capture_inputs: false`` the
    artifact has no inputs.json and llm_calls.jsonl carries no prompt
    messages — while hashes and structural metadata remain."""
    project_dir = tmp_path / "hello_no_capture"
    shutil.copytree(HELLO_DIR, project_dir)
    system_yaml = project_dir / "system.yaml"
    system_yaml.write_text(
        system_yaml.read_text() + "\nobservability:\n  capture_inputs: false\n"
    )

    transport = Transport(_tool_turns())
    code = execute_run(project_dir, '{"name": "world"}', transport=transport.build())
    assert code == 0

    run_dir = _single_run_dir(tmp_path)
    events = _events(run_dir)

    # the gate: no inputs.json, and no prompt content anywhere
    assert not (run_dir / "inputs.json").exists()
    llm_calls = [
        json.loads(line)
        for line in (run_dir / "llm_calls.jsonl").read_text().splitlines()
    ]
    assert llm_calls
    assert all(record["prompt_messages"] is None for record in llm_calls)
    for event in events:
        if event["event"] == "llm.started":
            assert event["prompt_messages"] is None

    # correlation stays: hashes + the rest of the artifact are intact
    started = next(e for e in events if e["event"] == "run.started")
    assert started["inputs_hash"]
    assert (run_dir / "outputs.json").exists()
    assert (run_dir / "metadata.json").exists()


@pytest.mark.integration
def test_metric_stream_isolates_project_cost(
    tmp_path: Path,
    metric_reader: InMemoryMetricReader,
    capsys: pytest.CaptureFixture[str],
    read_metric_points_fn: Any,
) -> None:
    transport = Transport(_tool_turns())
    assert execute_run(
        HELLO_DIR, '{"name": "world"}', transport=transport.build()
    ) == 0
    capsys.readouterr()
    run_dir = _single_run_dir(tmp_path)
    llm_events = [e for e in _events(run_dir) if e["event"] == "llm.completed"]
    expected_cost = sum(
        float(e["cost_estimate_usd"]) for e in llm_events
        if e.get("cost_estimate_usd") is not None
    )
    points = read_metric_points_fn(metric_reader, "foundry.llm.cost_usd")
    hello_cost = sum(v for attrs, v in points if attrs.get("project") == "hello")
    # cumulative counter: at least this run's cost, attributable to hello
    assert hello_cost >= expected_cost * 0.999
    assert any(
        attrs.get("model") == "gpt-5-mini"
        for attrs, _ in points
        if attrs.get("project") == "hello"
    )
