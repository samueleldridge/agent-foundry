"""Phase 9 pre-work guards (carried from the Phase 8 re-review):

1. binary WebSocket frames get a structured error frame, not a crash;
2. run-artifact routes refuse another project's runs under a shared
   FOUNDRY_HOME (ownership check mirrors deliver_approval);
3. batch items respect can_accept() — max_concurrent_runs + drain state
   apply per item, not just at batch admission;
4. request-size guard (structured 413) + batch-size cap.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from api_helpers import (
    HELLO_DIR,
    REPO_ROOT,
    hello_transport,
    parse_sse,
)
from starlette.testclient import TestClient

from foundry.api import create_app

FOREIGN_RUN = "01BBBBBBBBBBBBBBBBBBBBBBBB"  # another project's run
OWNED_RUN = "01CCCCCCCCCCCCCCCCCCCCCCCC"  # this project's run


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


def _write_artifact(
    tmp_path: Path, run_id: str, project: str, status: str = "completed"
) -> None:
    run_dir = tmp_path / "foundry_home" / "runs" / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "metadata.json").write_text(
        json.dumps(
            {"run_id": run_id, "project": project, "status": status}
        )
    )
    (run_dir / "events.jsonl").write_text(
        json.dumps(
            {
                "event": "run.started",
                "run_id": run_id,
                "sequence": 0,
                "timestamp": "2026-07-01T00:00:00Z",
                "worker_id": "elsewhere:1",
                "project": project,
                "system_version": "x",
                "pin_set_hash": "y",
                "inputs_hash": "z",
            }
        )
        + "\n"
    )


# --- 1. binary WebSocket frames ---------------------------------------------------


@pytest.mark.integration
def test_websocket_binary_frame_gets_structured_error() -> None:
    """A binary frame must produce the structured error frame and leave
    the socket serving — starlette's receive_json raises KeyError('text')
    on binary frames, which previously escaped as a server traceback."""
    app = create_app(HELLO_DIR, transport=hello_transport())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()  # welcome
        ws.send_bytes(b"\x00\x01\x02\xff")
        frame = ws.receive_json()
        assert frame["direction"] == "outbound"
        assert "binary" in frame["error"]["message"]
        # The socket still serves: a normal (erroring) inbound round-trips.
        ws.send_json(
            {
                "direction": "inbound",
                "message": {
                    "kind": "cancel",
                    "run_id": "01AAAAAAAAAAAAAAAAAAAAAAAA",
                    "client_sequence": 0,
                },
            }
        )
        frame = ws.receive_json()
        assert "not active" in frame["error"]["message"]


# --- 2. cross-project ownership under a shared FOUNDRY_HOME ------------------------


@pytest.mark.integration
def test_run_routes_refuse_other_projects_artifacts(tmp_path: Path) -> None:
    _write_artifact(tmp_path, FOREIGN_RUN, "other_project")
    _write_artifact(tmp_path, OWNED_RUN, "hello")
    app = create_app(HELLO_DIR, transport=hello_transport())
    with TestClient(app) as client:
        # Foreign artifacts read as not-found on every artifact route.
        assert client.get(f"/runs/{FOREIGN_RUN}").status_code == 404
        assert client.get(f"/runs/{FOREIGN_RUN}/events").status_code == 404
        resume = client.post(
            f"/runs/{FOREIGN_RUN}/resume",
            json={
                "kind": "resume",
                "run_id": FOREIGN_RUN,
                "client_sequence": 0,
            },
        )
        assert resume.status_code == 404

        # Positive control: this project's artifact stays readable.
        owned = client.get(f"/runs/{OWNED_RUN}")
        assert owned.status_code == 200
        assert owned.json()["project"] == "hello"
        events = client.get(f"/runs/{OWNED_RUN}/events")
        assert events.status_code == 200
        replay = parse_sse(events.text)
        assert replay[0]["data"]["event"] == "run.started"


@pytest.mark.integration
def test_events_route_checks_project_when_metadata_is_absent(
    tmp_path: Path,
) -> None:
    """An in-flight run (no metadata.json yet) is identified by its
    persisted run.started event — foreign ones still read as 404."""
    for run_id, project in ((FOREIGN_RUN, "other_project"), (OWNED_RUN, "hello")):
        _write_artifact(tmp_path, run_id, project)
        (
            tmp_path / "foundry_home" / "runs" / run_id / "metadata.json"
        ).unlink()
    app = create_app(HELLO_DIR, transport=hello_transport())
    with TestClient(app) as client:
        assert client.get(f"/runs/{FOREIGN_RUN}/events").status_code == 404
        assert client.get(f"/runs/{OWNED_RUN}/events").status_code == 200


# --- 3. batch items respect worker capacity + drain state --------------------------


@pytest.mark.integration
def test_batch_items_respect_max_concurrent_runs(tmp_path: Path) -> None:
    """max_parallel may exceed the worker's max_concurrent_runs; items must
    wait for capacity rather than bypass can_accept()."""
    app = create_app(
        HELLO_DIR, transport=hello_transport(), max_concurrent_runs=1
    )
    with TestClient(app) as client:
        manager = client.app.state.manager  # type: ignore[attr-defined]
        original = manager.start_run
        over_capacity: list[int] = []

        def guarded(input_data: dict[str, Any], **kwargs: Any) -> Any:
            if manager.active_count() >= manager.max_concurrent_runs:
                over_capacity.append(manager.active_count())
            return original(input_data, **kwargs)

        manager.start_run = guarded  # type: ignore[method-assign]
        response = client.post(
            "/batch",
            json={
                "items": [
                    {"item_id": f"i{n}", "input": {"name": f"n{n}"}}
                    for n in range(3)
                ],
                "policy": {"max_parallel": 3},
            },
        )
        assert response.status_code == 200
        frames = parse_sse(response.text)
    summary = frames[-1]["data"]
    assert summary["event"] == "batch.completed"
    assert summary["succeeded"] == 3
    assert over_capacity == [], (
        "batch items started runs while the worker was already at "
        f"max_concurrent_runs: {over_capacity}"
    )


@pytest.mark.integration
async def test_batch_items_fast_fail_when_worker_drains(
    tmp_path: Path,
) -> None:
    """Items admitted while the worker drains must fast-fail resumably
    (synthetic run.cancelled reason=worker_drain), not start runs."""
    from foundry.api.batch import BatchRequest, execute_batch

    app = create_app(HELLO_DIR, transport=hello_transport())
    async with app.router.lifespan_context(app):
        manager = app.state.manager
        manager.worker_state.draining = True
        request = BatchRequest.model_validate(
            {
                "items": [
                    {"item_id": f"i{n}", "input": {"name": f"n{n}"}}
                    for n in range(3)
                ],
            }
        )
        chunks = [chunk async for chunk in execute_batch(manager, request)]
        manager.worker_state.draining = False
    frames = [f for chunk in chunks for f in parse_sse(chunk)]
    cancelled = [
        f["data"]
        for f in frames
        if f["data"].get("event") == "run.cancelled"
    ]
    assert len(cancelled) == 3
    assert all(f["reason"] == "worker_drain" for f in cancelled)
    summary = frames[-1]["data"]
    assert summary["event"] == "batch.completed"
    assert summary["cancelled"] == 3
    assert summary["succeeded"] == 0


# --- 4. request-size guard + batch cap --------------------------------------------


@pytest.mark.integration
def test_oversized_body_yields_structured_413() -> None:
    app = create_app(
        HELLO_DIR, transport=hello_transport(), max_body_bytes=1024
    )
    with TestClient(app) as client:
        response = client.post("/run", json={"name": "x" * 4096})
        assert response.status_code == 413
        body = response.json()
        assert body["error_class"] == "RequestTooLarge"
        assert body["context"]["max_body_bytes"] == 1024
        # A normal-sized request still works.
        ok = client.post("/run", json={"name": "sam"})
        assert ok.status_code == 200


@pytest.mark.integration
def test_chunked_body_without_content_length_is_still_capped() -> None:
    """A chunked upload can't lie its way past the header check — the
    counted receive stream enforces the cap on actual bytes."""
    app = create_app(
        HELLO_DIR, transport=hello_transport(), max_body_bytes=1024
    )

    def chunks() -> Iterator[bytes]:
        payload = json.dumps({"name": "x" * 4096}).encode()
        for i in range(0, len(payload), 512):
            yield payload[i : i + 512]

    with TestClient(app) as client:
        response = client.post(
            "/run",
            content=chunks(),
            headers={"Content-Type": "application/json"},
        )
        assert response.status_code == 413
        assert response.json()["error_class"] == "RequestTooLarge"


@pytest.mark.integration
def test_batch_size_cap_yields_structured_413(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_MAX_BATCH_ITEMS", "5")
    app = create_app(HELLO_DIR, transport=hello_transport())
    with TestClient(app) as client:
        response = client.post(
            "/batch",
            json={
                "items": [
                    {"item_id": f"i{n}", "input": {"name": f"n{n}"}}
                    for n in range(6)
                ],
            },
        )
        assert response.status_code == 413
        body = response.json()
        assert body["error_class"] == "RequestTooLarge"
        assert body["context"]["max_batch_items"] == 5
