"""Phase 10a exit-gate: chat round-trip over the session SSE — llm.delta
streaming, approval.required → resume → run.completed, Last-Event-ID
replay (docs/03 § Phase 10a; mock transport — no LLM spend)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from api_helpers import hello_transport
from studio_helpers import (
    TEAM_INPUT,
    make_studio_repo,
    sse_events,
    stream_sse,
    team_transport,
)

from foundry.studio.app import create_studio_app

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = make_studio_repo(tmp_path, projects=("hello", "team_hello"))
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(repo / "catalog"))
    return repo


async def _lifespan_client(app: Any) -> Any:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://studio"
    )


async def test_chat_message_streams_llm_deltas_over_session_sse(
    repo: Path,
) -> None:
    # api_helpers.hello_transport: openai-shaped, input-reflecting.
    app = create_studio_app(
        repo, transport=hello_transport(), serve_assets=False
    )
    async with app.router.lifespan_context(app):
        async with await _lifespan_client(app) as client:
            opened = await client.post("/api/chat/hello/sessions")
            assert opened.status_code == 201
            session = opened.json()
            assert session["multi_turn"] is False
            sid = session["session_id"]

            posted = await client.post(
                f"/api/chat/hello/sessions/{sid}/messages",
                json={"text": '{"name": "studio"}'},
            )
            assert posted.status_code == 200
            run_id = posted.json()["run_id"]

            frames = await stream_sse(
                app,
                f"/api/chat/hello/sessions/{sid}/events",
                stop_when=lambda f: f.get("event") == "run.completed",
            )
            events = sse_events(frames)
            assert events[0] == "run.started"
            assert "llm.delta" in events
            assert events[-1] == "run.completed"
            assert frames[-1]["data"]["status"] == "success"
            assert frames[-1]["data"]["final_output"] == {
                "greeting": "Hello, studio!"
            }
            # Every frame belongs to the message's run and carries the
            # session-scoped id used by Last-Event-ID.
            assert all(f["data"]["run_id"] == run_id for f in frames)
            assert [f["id"] for f in frames] == list(range(len(frames)))

            # Last-Event-ID resume: only the missed events come back.
            resumed = await stream_sse(
                app,
                f"/api/chat/hello/sessions/{sid}/events",
                headers={"Last-Event-ID": "3"},
                stop_when=lambda f: f.get("event") == "run.completed",
            )
            assert [f["id"] for f in resumed] == list(
                range(4, len(frames))
            )

            # Session list shows the run for reattach.
            sessions = (
                await client.get("/api/chat/hello/sessions")
            ).json()
            assert sessions[0]["run_ids"] == [run_id]


async def test_chat_approval_round_trip(repo: Path) -> None:
    """approval.required surfaces over the session SSE; posting the
    approval resumes to run.completed(status=success)."""
    app = create_studio_app(
        repo, transport=team_transport(), serve_assets=False
    )
    async with app.router.lifespan_context(app):
        async with await _lifespan_client(app) as client:
            sid = (
                await client.post("/api/chat/team_hello/sessions")
            ).json()["session_id"]
            import json

            posted = await client.post(
                f"/api/chat/team_hello/sessions/{sid}/messages",
                json={"text": json.dumps(TEAM_INPUT)},
            )
            assert posted.status_code == 200

            frames = await stream_sse(
                app,
                f"/api/chat/team_hello/sessions/{sid}/events",
                stop_when=lambda f: f.get("event") == "approval.required",
            )
            approval = frames[-1]["data"]
            assert approval["agent_name"] == "publisher"
            approval_id = approval["approval_id"]

            # The pause shows in the approvals inbox too.
            inbox = (
                await client.get(
                    "/api/approvals", params={"project": "team_hello"}
                )
            ).json()
            assert inbox and inbox[0]["approval_id"] == approval_id

            resolved = await client.post(
                f"/api/chat/team_hello/sessions/{sid}/approvals",
                json={"approval_id": approval_id, "decision": "approved"},
            )
            assert resolved.status_code == 200

            def _final_success(frame: dict[str, Any]) -> bool:
                return (
                    frame.get("event") == "run.completed"
                    and frame["data"].get("status") == "success"
                )

            frames = await stream_sse(
                app,
                f"/api/chat/team_hello/sessions/{sid}/events",
                stop_when=_final_success,
            )
            events = sse_events(frames)
            assert "approval.required" in events
            assert "approval.resolved" in events
            assert events[-1] == "run.completed"
            assert frames[-1]["data"]["status"] == "success"
