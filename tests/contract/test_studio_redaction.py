"""Credential-leak contract test for the studio control plane (docs/72 §
Security posture, rule 5): a planted fake credential in the fixture
connection appears in ZERO route responses — connections, runs,
artifacts, forge listings, config snapshots, obs, everything.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tests" / "integration"))

from studio_helpers import make_studio_repo, stream_sse  # noqa: E402

PLANTED_SECRET = "studio-planted-fake-credential-a1b2c3d4e5f6"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    # THE PLANT: the fixture connection's credential env var.
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", PLANTED_SECRET)


def _hello_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps({"greeting": "Hello, leak!"}),
                    }
                ],
                "stop_reason": "end_turn",
                "model": "claude-haiku-4-5",
                "usage": {"input_tokens": 50, "output_tokens": 20},
            },
        )

    return httpx.MockTransport(handler)


@pytest.mark.contract
async def test_planted_credential_reaches_zero_route_responses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from foundry.studio.app import create_studio_app

    repo = make_studio_repo(tmp_path)
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(repo / "catalog"))
    app = create_studio_app(
        repo, transport=_hello_transport(), serve_assets=False
    )
    bodies: dict[str, str] = {}
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://s"
        ) as client:
            # Produce run artifacts + mirror rows through the chat path.
            sid = (
                await client.post("/api/chat/hello/sessions")
            ).json()["session_id"]
            run_id = (
                await client.post(
                    f"/api/chat/hello/sessions/{sid}/messages",
                    json={"text": '{"name": "leakcheck"}'},
                )
            ).json()["run_id"]
            frames = await stream_sse(
                app,
                f"/api/chat/hello/sessions/{sid}/events",
                stop_when=lambda f: f.get("event") == "run.completed",
            )
            bodies["chat sse"] = json.dumps(frames)

            get_routes = [
                "/api/health",
                "/api/projects",
                "/api/projects/hello",
                "/api/projects/hello/files",
                "/api/projects/hello/files/system.yaml",
                "/api/projects/hello/versions",
                "/api/projects/hello/compute-version",
                "/api/projects/hello/connections",
                "/api/projects/hello/connections/time_service",
                "/api/projects/hello/graph",
                "/api/catalog",
                "/api/catalog/connections/http_service",
                "/api/obs/cost",
                "/api/obs/latency",
                "/api/obs/tool-failures",
                "/api/obs/eval-trend",
                "/api/obs/runs",
                "/api/storage/stats",
                "/api/storage/pins",
                "/api/runs",
                f"/api/runs/{run_id}",
                f"/api/runs/{run_id}/artifact",
                "/api/approvals",
                "/api/evals",
                "/api/forge",
                "/api/layouts",
                "/api/chat/hello/sessions",
                "/api/doctor",
                "/api/openapi.json",
            ]
            for route in get_routes:
                response = await client.get(route)
                assert response.status_code < 500, (route, response.text)
                bodies[route] = response.text

            run_events = await client.get(f"/api/runs/{run_id}/events")
            bodies["run events sse"] = run_events.text

    hits = {
        route: body
        for route, body in bodies.items()
        if PLANTED_SECRET in body
    }
    assert hits == {}, f"credential leaked via: {sorted(hits)}"

    # Sanity: the plant is real — it IS in the process environment and
    # reachable by the connection layer (the test would be vacuous
    # otherwise).
    import os

    assert os.environ["HELLO_SERVICE_API_KEY"] == PLANTED_SECRET
