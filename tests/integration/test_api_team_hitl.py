"""Phase 8 exit-gate: WebSocket HITL against projects/team_hello.

A tool raises ApprovalRequired mid-run → the client receives
approval.required over the socket → sends ApprovalResponse →
approval.resolved → the run resumes to run.completed. Also the
non-streaming shape: POST /run answers 409 approval_pending; POST
/runs/{id}/resume with the ApprovalResponse completes the run.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_helpers import REPO_ROOT, TEAM_DIR, read_artifact_events
from starlette.testclient import TestClient

from foundry.api import create_app

RUN_INPUT = {"request": "the new release shipping", "audience": "the team"}


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


# --- scripted per-agent transport (the Phase 7 pattern) ---------------------------

_AGENT_MARKERS = {
    "coordinator": "coordinator — system prompt",
    "drafter": "drafter — system prompt",
    "publisher": "publisher — system prompt",
}


def _turn(*blocks: dict[str, Any], stop: str = "end_turn") -> dict[str, Any]:
    return {
        "content": list(blocks),
        "stop_reason": stop,
        "model": "claude-haiku-4-5",
        "usage": {"input_tokens": 60, "output_tokens": 30},
    }


def _text(payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "text", "text": json.dumps(payload)}


def _tool_use(name: str, inputs: dict[str, Any], block_id: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": inputs}


def _team_turns() -> dict[str, list[dict[str, Any]]]:
    return {
        "coordinator": [
            _turn(
                _tool_use(
                    "transfer_to_drafter", {"reason": "draft the greeting"},
                    "tu_c1",
                ),
                stop="tool_use",
            ),
            _turn(
                _tool_use(
                    "transfer_to_publisher", {"reason": "publish the draft"},
                    "tu_c2",
                ),
                stop="tool_use",
            ),
            _turn(
                _tool_use(
                    "transfer_to_end", {"reason": "published; all done"},
                    "tu_c3",
                ),
                stop="tool_use",
            ),
            _turn(
                _text(
                    {
                        "final_summary": "Drafted and published the release "
                        "greeting (publish_status: published)."
                    }
                )
            ),
        ],
        "drafter": [
            _turn(_text({"draft": "Hello team - the release shipped!"})),
        ],
        "publisher": [
            _turn(
                _tool_use(
                    "publish_greeting",
                    {"text": "Hello team - the release shipped!"},
                    "tu_p1",
                ),
                stop="tool_use",
            ),
            _turn(_text({"publish_status": "published"})),
        ],
    }


def _team_transport() -> httpx.MockTransport:
    turns = {agent: list(queue) for agent, queue in _team_turns().items()}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = body.get("system", "")
        agent = next(
            agent
            for agent, marker in _AGENT_MARKERS.items()
            if marker in system
        )
        queue = turns[agent]
        assert queue, f"no scripted turn left for {agent}"
        return httpx.Response(200, json=queue.pop(0))

    return httpx.MockTransport(handler)


def _copy_team(tmp_path: Path) -> Path:
    project = tmp_path / "team_hello"
    shutil.copytree(TEAM_DIR, project)
    return project


def _recv_until(ws: Any, predicate: Any, limit: int = 300) -> list[dict[str, Any]]:
    frames: list[dict[str, Any]] = []
    for _ in range(limit):
        frame = ws.receive_json()
        frames.append(frame)
        if predicate(frame):
            return frames
    raise AssertionError("predicate never satisfied")


def _is_event(frame: dict[str, Any], name: str) -> bool:
    return bool(frame.get("event") and frame["event"].get("event") == name)


@pytest.mark.integration
def test_websocket_hitl_approval_flow(tmp_path: Path) -> None:
    """Exit gate: approval.required event over WS → ApprovalResponse →
    approval.resolved → run.completed; one continuous sequence."""
    project = _copy_team(tmp_path)
    app = create_app(project, transport=_team_transport())
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        ws.receive_json()  # welcome
        ws.send_json(
            {
                "direction": "inbound",
                "message": {
                    "kind": "init_run",
                    "client_sequence": 0,
                    "input": RUN_INPUT,
                },
            }
        )
        frames = _recv_until(ws, lambda f: _is_event(f, "approval.required"))
        approval = frames[-1]["event"]
        run_id = approval["run_id"]
        assert approval["agent_name"] == "publisher"
        assert "Publish this greeting" in approval["prompt"]

        # The pause is visible as a status while the socket stays open.
        frames = _recv_until(ws, lambda f: _is_event(f, "run.completed"))
        assert frames[-1]["event"]["status"] == "approval_pending"
        status = client.get(f"/runs/{run_id}").json()
        assert status["status"] == "approval_pending"
        assert status["pending_approval"]["approval_id"] == (
            approval["approval_id"]
        )

        ws.send_json(
            {
                "direction": "inbound",
                "message": {
                    "kind": "approval_response",
                    "run_id": run_id,
                    "client_sequence": 1,
                    "approval_id": approval["approval_id"],
                    "decision": "approved",
                    "reason": "verified by desk head",
                },
            }
        )
        frames = _recv_until(ws, lambda f: _is_event(f, "approval.resolved"))
        resolved = frames[-1]["event"]
        assert resolved["decision"] == "approved"
        frames = _recv_until(
            ws,
            lambda f: _is_event(f, "run.completed")
            and f["event"]["status"] == "success",
        )
        final = frames[-1]["event"]["final_output"]
        assert "published" in final["final_summary"]

    # One continuous, monotonic event sequence across the pause.
    events = read_artifact_events(tmp_path, run_id)
    sequences = [e["sequence"] for e in events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    names = [e["event"] for e in events]
    assert names.count("approval.required") == 1
    assert names.count("approval.resolved") == 1


@pytest.mark.integration
def test_non_streaming_hitl_409_then_resume_completes(tmp_path: Path) -> None:
    """docs/70: ApprovalRequired on POST /run → 409 with the pending
    approval; POST /runs/{id}/resume with the ApprovalResponse completes
    and returns the typed output."""
    project = _copy_team(tmp_path)
    app = create_app(project, transport=_team_transport())
    with TestClient(app) as client:
        response = client.post("/run", json=RUN_INPUT)
        assert response.status_code == 409
        body = response.json()
        run_id = body["run_id"]
        assert body["status"] == "approval_pending"
        approval_id = body["pending_approval"]["approval_id"]
        assert body["resume_url"] == f"/runs/{run_id}/resume"

        resumed = client.post(
            f"/runs/{run_id}/resume",
            json={
                "kind": "approval_response",
                "run_id": run_id,
                "client_sequence": 0,
                "approval_id": approval_id,
                "decision": "approved",
                "reason": "ship it",
            },
        )
        assert resumed.status_code == 200
        assert "published" in resumed.json()["final_summary"]
        assert client.get(f"/runs/{run_id}").json()["status"] == "completed"
