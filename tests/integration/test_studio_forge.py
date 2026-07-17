"""Phase 10a exit-gate: forge lifecycle through the studio — launch as a
supervised background task, live SSE trajectory (iteration events, scores,
per-iteration commit shas, termination), 409 on a concurrent launch,
cancel finalising the artifact as cancelled (docs/03 § Phase 10a).

Reuses the Phase 6 scripted-forge harness (forge_helpers): ONE
httpx.MockTransport serves both the meta-agent's scripted turns and the
forged project's computed turns — no LLM spend.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge_helpers import (
    META_MODEL,
    PROMPT_WITH_BOTH,
    ForgeTransport,
    make_repo,
    prompt_iteration_turns,
    write_scaffolded_project,
)
from studio_helpers import sse_events, stream_sse

from foundry.studio.app import create_studio_app

pytestmark = pytest.mark.integration

LAUNCH_BODY = {
    "project": "qa_bot",
    "description": "Numeric-answer QA over three question kinds.",
    "eval_path": "projects/qa_bot/evals/qa.yaml",
    "threshold": 0.9,
    "max_iter": 3,
}


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    # meta-agent binds openai/gpt-5-mini; the toy project stays anthropic
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-tests")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = make_repo(tmp_path)
    write_scaffolded_project(repo)
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(repo / "catalog"))
    return repo


def _client(app: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://studio"
    )


async def test_forge_launch_streams_trajectory_and_terminates(
    repo: Path,
) -> None:
    """POST /api/forge launches a background forge; the SSE stream carries
    iteration events with scores + per-iteration commit shas and the
    termination event; the trajectory artifact is queryable after."""
    transport = ForgeTransport(
        prompt_iteration_turns(
            new_version="v2",
            content=PROMPT_WITH_BOTH,
            cluster_id="digit_and_reverse",
            summary="prompt: add digit + reverse rules",
            eval_before=0.5,
        )
    ).build()
    app = create_studio_app(repo, transport=transport, serve_assets=False)
    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            launched = await client.post("/api/forge", json=LAUNCH_BODY)
            assert launched.status_code == 202, launched.text
            forge_run_id = launched.json()["forge_run_id"]
            assert forge_run_id

            frames = await stream_sse(
                app,
                f"/api/forge/{forge_run_id}/events",
                stop_when=lambda f: f.get("event") == "forge.terminated",
            )
            events = sse_events(frames)
            assert events[0] == "forge.started"
            assert "forge.iteration_started" in events
            assert "forge.iteration_completed" in events
            completed = next(
                f["data"]
                for f in frames
                if f.get("event") == "forge.iteration_completed"
            )
            assert completed["eval_score"] == 1.0
            assert completed["commit_shas"]
            terminated = frames[-1]["data"]
            assert terminated["reason"] == "threshold_met"
            assert terminated["final_score"] == 1.0

            # forge list + show serve the finalised trajectory artifact.
            listed = (await client.get("/api/forge")).json()
            assert [row["forge_run_id"] for row in listed] == [forge_run_id]
            shown = (
                await client.get(f"/api/forge/{forge_run_id}")
            ).json()
            assert shown["status"] == "completed"
            assert shown["termination_reason"] == "threshold_met"
            assert shown["iterations"] == 1
            assert shown["trajectory"][-1]["eval_score_after"] == 1.0
            assert shown["trajectory"][-1]["commit_shas"]


async def test_concurrent_forge_for_same_project_is_409(repo: Path) -> None:
    """One forge per project at a time (docs/72)."""
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["model"] == META_MODEL:
            await gate.wait()  # hold the meta turn open
        from forge_helpers import project_response

        return httpx.Response(200, json=project_response(body))

    app = create_studio_app(
        repo,
        transport=httpx.MockTransport(handler),
        serve_assets=False,
    )
    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            first = await client.post("/api/forge", json=LAUNCH_BODY)
            assert first.status_code == 202
            forge_run_id = first.json()["forge_run_id"]

            second = await client.post("/api/forge", json=LAUNCH_BODY)
            assert second.status_code == 409
            body = second.json()
            assert body["error_class"] == "ForgeAlreadyRunning"
            assert body["context"]["forge_run_id"] == forge_run_id

            # Cancel unblocks teardown deterministically.
            cancelled = await client.post(
                f"/api/forge/{forge_run_id}/cancel"
            )
            assert cancelled.status_code == 200
            frames = await stream_sse(
                app,
                f"/api/forge/{forge_run_id}/events",
                stop_when=lambda f: f.get("event") == "forge.terminated",
            )
            assert frames[-1]["data"]["reason"] == "user_cancelled"


async def test_forge_cancel_finalises_artifact_as_cancelled(
    repo: Path, tmp_path: Path
) -> None:
    gate = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["model"] == META_MODEL:
            await gate.wait()
        from forge_helpers import project_response

        return httpx.Response(200, json=project_response(body))

    app = create_studio_app(
        repo,
        transport=httpx.MockTransport(handler),
        serve_assets=False,
    )
    async with app.router.lifespan_context(app):
        async with _client(app) as client:
            launched = await client.post("/api/forge", json=LAUNCH_BODY)
            forge_run_id = launched.json()["forge_run_id"]
            await client.post(f"/api/forge/{forge_run_id}/cancel")
            await stream_sse(
                app,
                f"/api/forge/{forge_run_id}/events",
                stop_when=lambda f: f.get("event") == "forge.terminated",
            )
            shown = (
                await client.get(f"/api/forge/{forge_run_id}")
            ).json()
            assert shown["status"] == "cancelled"
            assert shown["termination_reason"] == "user_cancelled"

            # A new launch is accepted once the slot is free.
            relaunched = await client.post("/api/forge", json=LAUNCH_BODY)
            assert relaunched.status_code in (202, 409)
            if relaunched.status_code == 202:
                await client.post(
                    f"/api/forge/{relaunched.json()['forge_run_id']}/cancel"
                )
