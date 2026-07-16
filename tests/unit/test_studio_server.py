"""`foundry studio` runner + security wiring: non-loopback refusal,
asset resolution (separate frontend repo), bearer auth, layouts
round-trip, EventLog resume (docs/72 § CLI + § Security posture)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from foundry.studio.app import create_studio_app, resolve_assets_dir
from foundry.studio.server import execute_studio

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))
    monkeypatch.delenv("FOUNDRY_STUDIO_TOKEN", raising=False)
    monkeypatch.delenv("FOUNDRY_STUDIO_DIST", raising=False)


# --- CLI runner ------------------------------------------------------------------


@pytest.mark.unit
def test_non_loopback_bind_without_token_refuses_to_start(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = execute_studio(REPO_ROOT, host="0.0.0.0", open_browser=False)
    assert code == 2
    err = capsys.readouterr().err
    assert "refusing to bind non-loopback host" in err
    assert "FOUNDRY_STUDIO_TOKEN" in err


@pytest.mark.unit
def test_dev_mode_prints_vite_workflow(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured: dict[str, Any] = {}

    import uvicorn

    def fake_run(app: Any, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    code = execute_studio(
        REPO_ROOT, dev=True, open_browser=False, port=8411
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "agent-foundry-studio && npm run dev" in out
    assert captured["port"] == 8411
    assert captured["host"] == "127.0.0.1"


@pytest.mark.unit
def test_non_loopback_with_token_starts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    code = execute_studio(
        REPO_ROOT, host="0.0.0.0", auth_token="t0ken", open_browser=False
    )
    assert code == 0


# --- asset resolution (separate frontend repository) -------------------------------


@pytest.mark.unit
def test_assets_resolution_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo_root = tmp_path / "agent-foundry"
    repo_root.mkdir()
    # nothing anywhere → placeholder
    assert resolve_assets_dir(repo_root) is None
    # sibling frontend checkout's dist is the dev default
    sibling = tmp_path / "agent-foundry-studio" / "dist"
    sibling.mkdir(parents=True)
    (sibling / "index.html").write_text("<html>sibling</html>")
    assert resolve_assets_dir(repo_root) == sibling
    # FOUNDRY_STUDIO_DIST overrides
    override = tmp_path / "elsewhere" / "dist"
    override.mkdir(parents=True)
    (override / "index.html").write_text("<html>override</html>")
    monkeypatch.setenv("FOUNDRY_STUDIO_DIST", str(override))
    assert resolve_assets_dir(repo_root) == override


@pytest.mark.unit
def test_explicit_dist_override_is_authoritative(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FOUNDRY_STUDIO_DIST pointing at a non-build must serve the
    placeholder — never silently fall back to a sibling checkout."""
    from foundry.studio.app import resolve_assets_dir

    monkeypatch.setenv("FOUNDRY_STUDIO_DIST", str(tmp_path / "not-a-build"))
    assert resolve_assets_dir(REPO_ROOT) is None


@pytest.mark.unit
def test_placeholder_page_serves_when_no_frontend_built(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Isolate resolution from any sibling agent-foundry-studio/dist build:
    # an explicit override that holds no build is authoritative (see above).
    monkeypatch.setenv("FOUNDRY_STUDIO_DIST", str(tmp_path / "empty"))
    app = create_studio_app(REPO_ROOT)
    with TestClient(app) as client:
        response = client.get("/")
        assert response.status_code == 200
        assert "frontend is not built yet" in response.text
        assert "/api/openapi.json" in response.text
        # /api 404s stay structured JSON, never the SPA fallback.
        missing = client.get("/api/definitely-not-a-route")
        assert missing.status_code == 404
        assert missing.json()["error_class"] == "NotFound"


@pytest.mark.unit
def test_spa_fallback_serves_built_assets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<html>SPA</html>")
    (dist / "assets" / "app.js").write_text("console.log('studio')")
    monkeypatch.setenv("FOUNDRY_STUDIO_DIST", str(dist))
    app = create_studio_app(REPO_ROOT)
    with TestClient(app) as client:
        assert client.get("/assets/app.js").text == "console.log('studio')"
        # history fallback: any non-/api miss serves index.html
        assert client.get("/projects/hello/configs").text == (
            "<html>SPA</html>"
        )


# --- bearer auth -------------------------------------------------------------------


@pytest.mark.unit
def test_bearer_token_gates_every_api_route(tmp_path: Path) -> None:
    app = create_studio_app(
        REPO_ROOT, auth_token="s3cret", serve_assets=False
    )
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 401
        assert client.get("/api/projects").status_code == 401
        ok = client.get(
            "/api/health", headers={"Authorization": "Bearer s3cret"}
        )
        assert ok.status_code == 200
        # SSE / EventSource fallback: token via query param.
        assert client.get("/api/health?token=s3cret").status_code == 200
        bad = client.get(
            "/api/health", headers={"Authorization": "Bearer wrong"}
        )
        assert bad.status_code == 401


# --- layouts + EventLog ---------------------------------------------------------


@pytest.mark.unit
def test_layouts_put_persists_and_round_trips(tmp_path: Path) -> None:
    from foundry.storage.paths import foundry_home

    document = {
        "version": 1,
        "active": "default",
        "dashboards": {
            "default": {
                "widgets": [
                    {
                        "id": "w1",
                        "widget": "project-health",
                        "config": {"project": "hello"},
                        "layout": {"x": 0, "y": 0, "w": 4, "h": 3},
                    }
                ]
            }
        },
    }
    app = create_studio_app(REPO_ROOT, serve_assets=False)
    with TestClient(app) as client:
        put = client.put("/api/layouts", json=document)
        assert put.status_code == 200
        path = foundry_home() / "studio" / "layouts.json"
        assert path.is_file()
        got = client.get("/api/layouts").json()
    assert got["dashboards"] == document["dashboards"]
    assert got["active"] == "default"


@pytest.mark.unit
async def test_event_log_replays_then_hands_over_live() -> None:
    import asyncio

    from foundry.studio.events import EventLog, resume_sequence

    log = EventLog()
    log.append({"event": "a"})
    log.append({"event": "b"})

    collected: list[dict[str, Any]] = []

    async def consume() -> None:
        async for item in log.subscribe(from_sequence=1):
            collected.append(item)
            if item["event"] == "d":
                break

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.01)
    log.append({"event": "c"})
    log.append({"event": "d"})
    await asyncio.wait_for(task, 5)
    assert [item["event"] for item in collected] == ["b", "c", "d"]
    assert [item["sequence"] for item in collected] == [1, 2, 3]

    assert resume_sequence("41", 0) == 42
    assert resume_sequence(None, 7) == 7
    assert resume_sequence("junk", 7) == 7
