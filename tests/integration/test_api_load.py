"""Phase 8 exit-gate: sustained load (SCALED for CI) + graceful drain.

The full recipe — 100 runs/sec for 5 minutes across 4 workers sharing
Redis + Postgres — is the operator's manual step
(docs/_manual_tests/phase_8.md). The in-CI variant here: ~20 concurrent
submitters hammering one worker for FOUNDRY_LOAD_TEST_DURATION_S
(default 10s; the manual recipe raises it) against the mock provider,
asserting the docs/85 § Load test invariants that are measurable in one
process:

- 0 dropped events — every accepted run's artifact holds a contiguous
  sequence from run.started to a terminal event;
- p95 request latency stays sane;
- 0 orphan pool connections (acquires == releases per run);
- over-budget runs fast-fail cleanly (CostBudgetExceeded before any
  provider HTTP call).
"""

from __future__ import annotations

import json
import os
import shutil
import threading
import time
from pathlib import Path
from typing import Any

import pytest
from api_helpers import (
    HELLO_DIR,
    REPO_ROOT,
    hello_transport,
    read_artifact_events,
    read_artifact_metadata,
)
from starlette.testclient import TestClient

from foundry.api import create_app


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


@pytest.mark.integration
def test_sustained_load_no_dropped_events_sane_p95(tmp_path: Path) -> None:
    duration_s = float(os.environ.get("FOUNDRY_LOAD_TEST_DURATION_S", "10"))
    concurrency = 20
    app = create_app(
        HELLO_DIR,
        transport=hello_transport(),
        max_concurrent_runs=256,
        checkpoint="memory",  # per-run graphs; sqlite fsync isn't the SUT
    )
    results: list[tuple[int, float, str | None]] = []
    results_lock = threading.Lock()
    stop = threading.Event()

    with TestClient(app) as client:

        def submitter(worker_index: int) -> None:
            while not stop.is_set():
                started = time.monotonic()
                response = client.post(
                    "/run", json={"name": f"load worker {worker_index}"}
                )
                latency = time.monotonic() - started
                with results_lock:
                    results.append(
                        (
                            response.status_code,
                            latency,
                            response.headers.get("X-Foundry-Run-Id"),
                        )
                    )

        threads = [
            threading.Thread(target=submitter, args=(i,), daemon=True)
            for i in range(concurrency)
        ]
        for thread in threads:
            thread.start()
        time.sleep(duration_s)
        stop.set()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(t.is_alive() for t in threads), "submitters hung"

    assert len(results) >= concurrency * 2, "load generator barely ran"
    statuses = {code for code, _, _ in results}
    assert statuses == {200}, f"non-200s under load: {statuses}"

    latencies = sorted(latency for _, latency, _ in results)
    p95 = latencies[int(len(latencies) * 0.95)]
    assert p95 < 5.0, f"p95 latency {p95:.2f}s is not sane"

    # 0 dropped events: every accepted run has a contiguous artifact
    # stream from run.started to a terminal event, and pool counters
    # balance (0 orphan connections).
    run_ids = [run_id for _, _, run_id in results if run_id]
    assert len(run_ids) == len(results)
    sample = run_ids[:: max(1, len(run_ids) // 50)]
    for run_id in sample:
        events = read_artifact_events(tmp_path, run_id)
        assert events[0]["event"] == "run.started"
        assert events[-1]["event"] == "run.completed"
        assert [e["sequence"] for e in events] == list(range(len(events)))
        metadata = read_artifact_metadata(tmp_path, run_id)
        pool = metadata["connection_pool"]
        assert pool["acquires"] == pool["releases"], "orphan pool connection"

    print(
        f"\nsustained load: {len(results)} runs in {duration_s:.0f}s "
        f"({len(results) / duration_s:.1f} rps), p95 {p95 * 1000:.0f}ms"
    )


@pytest.mark.integration
def test_over_budget_runs_fast_fail_cleanly(tmp_path: Path) -> None:
    """docs/85: budget breaches fast-fail — CostBudgetExceeded fires
    pre-call (no provider HTTP), the artifact records run.failed, and the
    API answers quickly with the structured RunResult-error shape."""
    project = tmp_path / "hello_budget"
    shutil.copytree(HELLO_DIR, project)
    system_yaml = project / "system.yaml"
    system_yaml.write_text(
        system_yaml.read_text().replace(
            "guardrails:\n  max_iterations: 5",
            'guardrails:\n  max_iterations: 5\n  max_cost_usd: "0.000001"',
        )
    )
    http_calls = 0

    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json={})

    app = create_app(project, transport=httpx.MockTransport(handler))
    with TestClient(app) as client:
        latencies: list[float] = []
        for i in range(10):
            started = time.monotonic()
            response = client.post("/run", json={"name": f"pricey {i}"})
            latencies.append(time.monotonic() - started)
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "failed"
            assert body["error"]["error_class"] == "CostBudgetExceeded"
            run_id = body["run_id"]
        assert http_calls == 0, "budget must fire BEFORE any provider call"
        assert max(latencies) < 2.0, "over-budget runs must fail FAST"

    events = read_artifact_events(tmp_path, run_id)
    assert events[-1]["event"] == "run.failed"
    assert events[-1]["error"]["error_class"] == "CostBudgetExceeded"


@pytest.mark.integration
def test_graceful_shutdown_drains_in_flight_runs(tmp_path: Path) -> None:
    """docs/71 § Graceful shutdown: on lifespan exit the worker drains —
    in-flight runs finish inside the drain window, the app exits cleanly,
    and nothing is orphaned (the lifespan task group owns every run)."""
    import asyncio

    import httpx

    async def slowish(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.2)
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": json.dumps({"greeting": "hi"})}
                ],
                "stop_reason": "end_turn",
                "model": "claude-haiku-4-5",
                "usage": {"input_tokens": 5, "output_tokens": 5},
            },
        )

    app = create_app(
        HELLO_DIR,
        transport=httpx.MockTransport(slowish),
        drain_timeout_s=5.0,
    )
    responses: list[Any] = []
    with TestClient(app) as client:

        def submit() -> None:
            responses.append(client.post("/run", json={"name": "draining"}))

        thread = threading.Thread(target=submit, daemon=True)
        thread.start()
        # Wait until the run is actually in flight, then exit the client
        # context: lifespan shutdown drains while the 0.2s LLM call runs.
        manager = app.state.manager
        deadline = time.monotonic() + 5.0
        while manager.active_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert manager.active_count() == 1
    thread.join(timeout=10)
    assert responses and responses[0].status_code == 200
    run_id = responses[0].headers["X-Foundry-Run-Id"]
    events = read_artifact_events(tmp_path, run_id)
    assert events[-1]["event"] == "run.completed", (
        "the drain window must let the in-flight run finish"
    )
    metadata = read_artifact_metadata(tmp_path, run_id)
    assert metadata["status"] == "completed"


@pytest.mark.integration
def test_forced_drain_cancels_with_worker_drain_reason(tmp_path: Path) -> None:
    """docs/71 step 3: runs still in flight when the drain window closes
    are force-cancelled with reason=worker_drain; checkpoint persists."""
    from api_helpers import GatedTransport

    gate = GatedTransport(hang_calls=99)
    app = create_app(
        HELLO_DIR,
        transport=gate.build(),
        drain_timeout_s=0.2,  # tiny window: the hung run cannot finish
    )
    responses: list[Any] = []
    with TestClient(app) as client:

        def submit() -> None:
            responses.append(client.post("/run", json={"name": "stuck"}))

        thread = threading.Thread(target=submit, daemon=True)
        thread.start()
        manager = app.state.manager
        deadline = time.monotonic() + 5.0
        while manager.active_count() == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert manager.active_count() == 1
    thread.join(timeout=10)
    assert responses and responses[0].status_code == 499
    run_id = responses[0].json()["run_id"]
    events = read_artifact_events(tmp_path, run_id)
    assert events[-1]["event"] == "run.cancelled"
    assert events[-1]["reason"] == "worker_drain"
    metadata = read_artifact_metadata(tmp_path, run_id)
    assert metadata["status"] == "cancelled"
    assert metadata["checkpointer"] == "sqlite"
