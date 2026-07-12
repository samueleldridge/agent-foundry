"""Phase 8 exit-gate: POST /batch (docs/85 § Batch submission primitive).

20 inputs → per-item RunEvents over ONE SSE connection with correct
batch_id/item_id tagging; batch-level cost budget enforced (over-budget
items fast-fail cleanly with run.cancelled(reason=batch_budget_exceeded)).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from api_helpers import (
    HELLO_DIR,
    REPO_ROOT,
    hello_transport,
    parse_sse,
)
from starlette.testclient import TestClient

from foundry.api import create_app


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


def _items(n: int) -> list[dict[str, object]]:
    return [
        {"item_id": f"item_{i:03d}", "input": {"name": f"caller {i}"}}
        for i in range(n)
    ]


@pytest.mark.integration
def test_batch_of_20_streams_tagged_per_item_events(tmp_path: Path) -> None:
    """Exit gate: 20 inputs, one SSE connection, every per-item event
    tagged with batch_id + item_id, terminal batch.completed summary."""
    app = create_app(HELLO_DIR, transport=hello_transport())
    with TestClient(app) as client:
        response = client.post(
            "/batch",
            json={
                "batch_id": "01BATCHBATCHBATCHBATCHBATCH"[:26],
                "items": _items(20),
                "policy": {"max_parallel": 8},
            },
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        batch_id = response.headers["X-Foundry-Batch-Id"]
        frames = parse_sse(response.text)

    summary = frames[-1]
    assert summary["event"] == "batch.completed"
    assert summary["data"]["batch_id"] == batch_id
    assert summary["data"]["total"] == 20
    assert summary["data"]["succeeded"] == 20
    assert summary["data"]["failed"] == 0
    assert summary["data"]["budget_exceeded"] is False

    item_frames = [f for f in frames if f["event"] != "batch.completed"]
    assert all(f["data"]["batch_id"] == batch_id for f in item_frames)
    item_ids = {f["data"]["item_id"] for f in item_frames}
    assert item_ids == {f"item_{i:03d}" for i in range(20)}
    # Every item has a full run: started + terminal, in per-item order.
    for i in range(20):
        events = [
            f["data"]["event"]
            for f in item_frames
            if f["data"]["item_id"] == f"item_{i:03d}"
        ]
        assert events[0] == "run.started"
        assert events[-1] == "run.completed"
        assert "llm.delta" in events
    # The per-item outputs reflect the per-item inputs.
    outputs = {
        f["data"]["item_id"]: f["data"]["final_output"]["greeting"]
        for f in item_frames
        if f["data"]["event"] == "run.completed"
    }
    assert outputs["item_007"] == "Hello, caller 7!"


@pytest.mark.integration
def test_batch_cost_budget_fast_fails_remaining_items(tmp_path: Path) -> None:
    """Exit gate: batch-level cost budget enforced — the breach event
    precedes the cancellations (docs/85 invariant 8) and every unstarted
    item fast-fails with run.cancelled(batch_budget_exceeded)."""
    app = create_app(HELLO_DIR, transport=hello_transport())
    with TestClient(app) as client:
        response = client.post(
            "/batch",
            json={
                "items": _items(10),
                # max_parallel=1 makes the breach deterministic: item 0's
                # llm.completed cost trips the (tiny) budget before any
                # other item starts.
                "policy": {
                    "max_parallel": 1,
                    "max_cost_usd": "0.0000001",
                    "stop_on_budget_exceeded": True,
                },
            },
        )
        assert response.status_code == 200
        frames = parse_sse(response.text)

    names = [f["event"] for f in frames]
    assert names.count("batch.budget_exceeded") == 1
    breach_at = names.index("batch.budget_exceeded")
    summary = frames[-1]["data"]
    assert summary["budget_exceeded"] is True
    assert summary["succeeded"] == 1
    assert summary["cancelled"] == 9

    cancelled = [
        f
        for f in frames
        if f["event"] == "run.cancelled"
        and f["data"].get("reason") == "batch_budget_exceeded"
    ]
    assert len(cancelled) == 9
    # Invariant 8: the breach event lands before every cancellation.
    assert all(frames.index(f) > breach_at for f in cancelled)
    # Fast-fail means no run.started for the cancelled items.
    started_items = {
        f["data"]["item_id"]
        for f in frames
        if f["data"].get("event") == "run.started"
    }
    assert started_items == {"item_000"}


@pytest.mark.integration
def test_batch_items_validate_against_the_project_input_schema() -> None:
    app = create_app(HELLO_DIR, transport=hello_transport())
    with TestClient(app) as client:
        response = client.post(
            "/batch",
            json={
                "items": [{"item_id": "bad", "input": {"wrong_field": 1}}],
            },
        )
        assert response.status_code in (400, 422)
