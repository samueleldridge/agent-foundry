"""Phase 8 exit-gates: SSE streaming, Last-Event-ID reconnect, client-kill
→ cancel → resume, and the hello WebSocket round-trip (InjectInput /
CancelRun)."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from api_helpers import (
    HELLO_DIR,
    REPO_ROOT,
    GatedTransport,
    hello_transport,
    parse_sse,
    read_artifact_events,
    read_artifact_metadata,
    sse_events,
)
from starlette.testclient import TestClient

from foundry.api import create_app


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


def _wait_status(
    client: TestClient, run_id: str, wanted: set[str], timeout_s: float = 10.0
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        status = client.get(f"/runs/{run_id}").json()
        if status.get("status") in wanted:
            return status
        time.sleep(0.02)
    raise AssertionError(f"run {run_id} never reached {wanted}: {status}")


# --- SSE: progressive events + clean close -----------------------------------------


@pytest.mark.integration
def test_post_stream_emits_progressive_run_events(tmp_path: Path) -> None:
    """Exit gate: run.started -> llm.delta x N -> run.completed over SSE;
    connection closes cleanly; id: matches the sequence."""
    app = create_app(HELLO_DIR, transport=hello_transport())
    with TestClient(app) as client:
        with client.stream(
            "POST", "/stream", json={"name": "streamer"}
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith(
                "text/event-stream"
            )
            run_id = response.headers["X-Foundry-Run-Id"]
            body = "".join(response.iter_text())
    frames = parse_sse(body)
    names = sse_events(frames)
    assert names[0] == "run.started"
    assert names[-1] == "run.completed"
    assert names.count("llm.delta") >= 1
    assert names.index("llm.started") < names.index("llm.delta")
    assert names.index("llm.delta") < names.index("llm.completed")
    ids = [f["id"] for f in frames]
    assert ids == list(range(len(frames)))
    for frame in frames:
        assert frame["data"]["sequence"] == frame["id"]
        assert frame["data"]["run_id"] == run_id
    final = frames[-1]["data"]
    assert final["status"] == "success"
    assert final["final_output"]["greeting"] == "Hello, streamer!"


@pytest.mark.integration
def test_sse_reconnect_with_last_event_id_replays_from_n_plus_1(
    tmp_path: Path,
) -> None:
    """Exit gate: reconnect with Last-Event-ID: N replays sequence > N
    from the persisted artifact, byte-identical to what a continuous
    listener saw."""
    app = create_app(HELLO_DIR, transport=hello_transport())
    with TestClient(app) as client:
        with client.stream(
            "POST", "/stream", json={"name": "resume"}
        ) as response:
            run_id = response.headers["X-Foundry-Run-Id"]
            full = parse_sse("".join(response.iter_text()))
        # "Kill" happened after event 2; reconnect with Last-Event-ID.
        replay = client.get(
            f"/runs/{run_id}/events", headers={"Last-Event-ID": "2"}
        )
        assert replay.status_code == 200
        replayed = parse_sse(replay.text)
        assert [f["id"] for f in replayed] == [
            f["id"] for f in full if f["id"] > 2
        ]
        assert [f["data"] for f in replayed] == [
            f["data"] for f in full if f["id"] > 2
        ]
        # The query-param form works too (docs/70).
        replay2 = client.get(
            f"/runs/{run_id}/events", params={"from_sequence": 2}
        )
        assert [f["id"] for f in parse_sse(replay2.text)] == [
            f["id"] for f in replayed
        ]


# --- kill client mid-stream → cancel → status → resume ------------------------------


@pytest.mark.integration
async def test_kill_mid_stream_cancels_then_resume_completes(
    tmp_path: Path,
) -> None:
    """Exit gate: client disconnect mid-stream → server cancels the run
    (run.cancelled persisted, checkpoint intact) → GET /runs/{id} shows
    the status → POST /runs/{id}/resume finishes the run; the event
    sequence continues across the interruption.

    The in-process test transports (TestClient / httpx ASGITransport)
    BUFFER streaming responses fully, so the disconnect is exercised at
    the exact layer starlette drives on a real client drop: closing the
    response-body generator mid-stream (its finally is the
    cancel-on-disconnect contract). The live curl + Ctrl-C variant is the
    operator's manual step (docs/_manual_tests/phase_8.md)."""
    import asyncio

    import httpx

    from foundry.api.streaming import sse_run_stream

    gate = GatedTransport(hang_calls=1)
    app = create_app(HELLO_DIR, transport=gate.build())
    async with app.router.lifespan_context(app):
        manager = app.state.manager
        live = manager.start_run({"name": "cutoff"})
        run_id = str(live.run_id)

        # Consume SSE frames until the LLM call is in flight, then "kill
        # the client": aclose() the body generator (what starlette does
        # when the socket drops).
        generator = sse_run_stream(manager, run_id, 0, cancel_on_disconnect=True)
        seen: list[str] = []
        async for chunk in generator:
            for line in chunk.splitlines():
                if line.startswith("event: "):
                    seen.append(line.removeprefix("event: "))
            if "llm.started" in seen:
                break
        await generator.aclose()
        assert "run.completed" not in seen

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://api"
        ) as client:
            for _ in range(200):
                status = (await client.get(f"/runs/{run_id}")).json()
                if status["status"] == "cancelled":
                    break
                await asyncio.sleep(0.02)
            assert status["status"] == "cancelled"

            events = read_artifact_events(tmp_path, run_id)
            assert events[-1]["event"] == "run.cancelled"
            assert events[-1]["reason"] == "user_abort"
            metadata = read_artifact_metadata(tmp_path, run_id)
            assert metadata["status"] == "cancelled"
            assert metadata["checkpointer"] == "sqlite"

            # Resume: the checkpointer re-drives from the interrupted node.
            resumed = await client.post(
                f"/runs/{run_id}/resume",
                json={
                    "kind": "resume",
                    "run_id": run_id,
                    "client_sequence": 0,
                },
            )
            assert resumed.status_code == 200
            assert resumed.json() == {"greeting": "Hello, late world!"}

    events = read_artifact_events(tmp_path, run_id)
    names = [e["event"] for e in events]
    assert names.count("run.cancelled") == 1
    assert names[-1] == "run.completed"
    sequences = [e["sequence"] for e in events]
    assert sequences == list(range(len(events)))
    # The graph resumed rather than restarting: one llm.completed total —
    # the killed drive never got its HTTP answer.
    assert names.count("llm.completed") == 1


@pytest.mark.integration
def test_explicit_cancel_via_resume_endpoint(tmp_path: Path) -> None:
    """Cancellation polish: POST /runs/{id}/resume kind=cancel on an
    in-flight run emits run.cancelled(user_abort); the SSE replay of the
    finished stream ends with it."""
    gate = GatedTransport(hang_calls=99)
    app = create_app(HELLO_DIR, transport=gate.build())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()  # welcome
        ws.send_json(
            {
                "direction": "inbound",
                "message": {
                    "kind": "init_run",
                    "client_sequence": 0,
                    "input": {"name": "x"},
                },
            }
        )
        frames = _recv_until(ws, lambda f: _is_event(f, "llm.started"))
        run_id = next(f["event"]["run_id"] for f in frames if "event" in f)
        cancel = client.post(
            f"/runs/{run_id}/resume",
            json={
                "kind": "cancel",
                "run_id": run_id,
                "client_sequence": 0,
                "reason": "user_abort",
            },
        )
        assert cancel.status_code == 200
        _recv_until(ws, lambda f: _is_event(f, "run.cancelled"))
        _wait_status(client, run_id, {"cancelled"})
        # The persisted stream replays to the run.cancelled terminal.
        replay = parse_sse(client.get(f"/runs/{run_id}/events").text)
        assert replay[-1]["event"] == "run.cancelled"
        assert replay[-1]["data"]["reason"] == "user_abort"


@pytest.mark.integration
def test_wall_time_timeout_cancels_with_reason_timeout(
    tmp_path: Path,
) -> None:
    """Cancellation polish: a Guardrails.max_wall_time_s breach cancels
    the run with reason=timeout; checkpoint + artifact persist."""
    import shutil

    project = tmp_path / "hello_timeout"
    shutil.copytree(HELLO_DIR, project)
    system_yaml = project / "system.yaml"
    system_yaml.write_text(
        system_yaml.read_text().replace(
            "guardrails:\n  max_iterations: 5",
            "guardrails:\n  max_iterations: 5\n  max_wall_time_s: 0.3",
        )
    )
    slow_gate = GatedTransport(hang_calls=99)
    slow_app = create_app(project, transport=slow_gate.build())
    with TestClient(slow_app) as client:
        run = client.post("/run", json={"name": "slow"})
        assert run.status_code == 499
        assert run.json()["status"] == "cancelled"
        assert run.json()["reason"] == "timeout"
        run_id = run.json()["run_id"]
    events = read_artifact_events(tmp_path, run_id)
    assert events[-1]["event"] == "run.cancelled"
    assert events[-1]["reason"] == "timeout"


# --- WebSocket: InjectInput + CancelRun ------------------------------------------


def _recv_until(ws: Any, predicate: Any, limit: int = 200) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for _ in range(limit):
        frame = ws.receive_json()
        frames.append(frame)
        if predicate(frame):
            return frames
    raise AssertionError(f"predicate never satisfied; got {len(frames)} frames")


def _is_event(frame: dict[str, Any], name: str) -> bool:
    return bool(frame.get("event") and frame["event"].get("event") == name)


@pytest.mark.integration
def test_websocket_inject_input_reflected_in_output(tmp_path: Path) -> None:
    """Exit gate: connect WS, send InjectInput, observe the injected input
    reflected in the run's subsequent deltas/output."""
    app = create_app(HELLO_DIR, transport=hello_transport())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        welcome = ws.receive_json()
        assert welcome["welcome"]["project"] == "hello"
        next_run_id = welcome["welcome"]["next_run_id"]
        ws.send_json(
            {
                "direction": "inbound",
                "message": {
                    "kind": "inject_input",
                    "run_id": next_run_id,
                    "client_sequence": 0,
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": '{"name": "Ada"}'}
                        ],
                    },
                },
            }
        )
        frames = _recv_until(ws, lambda f: _is_event(f, "run.completed"))
        events = [f["event"]["event"] for f in frames if "event" in f]
        assert "run.started" in events
        assert "llm.delta" in events
        deltas = [
            f["event"]["delta"]["text"]
            for f in frames
            if _is_event(f, "llm.delta")
        ]
        assert any("Ada" in d for d in deltas)
        final = frames[-1]["event"]
        assert final["final_output"] == {"greeting": "Hello, Ada!"}

        # Plain-text inject fills the single required input field.
        ws.send_json(
            {
                "direction": "inbound",
                "message": {
                    "kind": "inject_input",
                    "run_id": next_run_id,
                    "client_sequence": 1,
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": "Grace"}],
                    },
                },
            }
        )
        frames = _recv_until(ws, lambda f: _is_event(f, "run.completed"))
        assert frames[-1]["event"]["final_output"] == {
            "greeting": "Hello, Grace!"
        }


@pytest.mark.integration
def test_websocket_cancel_run_yields_run_cancelled(tmp_path: Path) -> None:
    """Exit gate: send CancelRun over the socket; observe run.cancelled."""
    gate = GatedTransport(hang_calls=99)
    app = create_app(HELLO_DIR, transport=gate.build())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()  # welcome
        ws.send_json(
            {
                "direction": "inbound",
                "message": {
                    "kind": "init_run",
                    "client_sequence": 0,
                    "input": {"name": "doomed"},
                },
            }
        )
        frames = _recv_until(ws, lambda f: _is_event(f, "llm.started"))
        run_id = next(
            f["event"]["run_id"] for f in frames if "event" in f
        )
        ws.send_json(
            {
                "direction": "inbound",
                "message": {
                    "kind": "cancel",
                    "run_id": run_id,
                    "client_sequence": 1,
                    "reason": "user_abort",
                },
            }
        )
        frames = _recv_until(ws, lambda f: _is_event(f, "run.cancelled"))
        cancelled = frames[-1]["event"]
        assert cancelled["reason"] == "user_abort"
        assert cancelled["run_id"] == run_id


@pytest.mark.integration
def test_websocket_error_frames_for_bad_inbound(tmp_path: Path) -> None:
    app = create_app(HELLO_DIR, transport=hello_transport())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()  # welcome
        ws.send_json({"direction": "inbound", "message": {"kind": "nope"}})
        frame = ws.receive_json()
        assert "error" in frame
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


@pytest.mark.integration
def test_websocket_survives_malformed_frames(tmp_path: Path) -> None:
    """Phase 8 review fix: malformed frames must produce a structured
    error frame (same shape as the unknown-kind error) and leave the
    socket serving — not tear it down with a server traceback."""
    app = create_app(HELLO_DIR, transport=hello_transport())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        welcome = ws.receive_json()
        next_run_id = welcome["welcome"]["next_run_id"]

        # (1) init_run whose input fails the project input model.
        ws.send_json(
            {
                "direction": "inbound",
                "message": {
                    "kind": "init_run",
                    "client_sequence": 0,
                    "input": {"wrong_field": 1},
                },
            }
        )
        frame = ws.receive_json()
        assert frame["direction"] == "outbound"
        assert "input model" in frame["error"]["message"]

        # (2) inject_input whose JSON text fails the input model.
        ws.send_json(
            {
                "direction": "inbound",
                "message": {
                    "kind": "inject_input",
                    "run_id": next_run_id,
                    "client_sequence": 1,
                    "message": {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": '{"wrong_field": 1}'}
                        ],
                    },
                },
            }
        )
        frame = ws.receive_json()
        assert frame["direction"] == "outbound"
        assert "input model" in frame["error"]["message"]

        # (3) a non-JSON text frame.
        ws.send_text("this is not json {")
        frame = ws.receive_json()
        assert frame["direction"] == "outbound"
        assert "not valid JSON" in frame["error"]["message"]

        # (4) a JSON frame that is not an object.
        ws.send_text('["not", "an", "object"]')
        frame = ws.receive_json()
        assert frame["direction"] == "outbound"
        assert "JSON object" in frame["error"]["message"]

        # The socket still works: a valid init_run runs to completion.
        ws.send_json(
            {
                "direction": "inbound",
                "message": {
                    "kind": "init_run",
                    "client_sequence": 2,
                    "input": {"name": "Survivor"},
                },
            }
        )
        frames = _recv_until(ws, lambda f: _is_event(f, "run.completed"))
        assert frames[-1]["event"]["final_output"] == {
            "greeting": "Hello, Survivor!"
        }
