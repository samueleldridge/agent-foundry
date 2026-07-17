"""Studio surfaces for a project whose runtime secrets are MISSING
(docs/72 § Failure modes): run-shaped routes return a 424
``ProjectUnavailableError`` envelope naming the env var + remedy, the
chat sessions list still returns stored sessions without compiling, and
the project detail exposes the ``unavailable`` block the UI banners."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from studio_helpers import make_studio_repo

from foundry.studio.app import create_studio_app

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _missing_secret_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    # hello binds openai/gpt-5-mini — the model key must be present so the
    # ONLY thing standing between the project and a compile is the
    # connection credential below.
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-tests")
    # hello's connection credentials_ref names this env var — unset, the
    # project compiles nowhere in this process (the shape the operator
    # report hit with rag_hello / COHERE_API_KEY before rag_hello went
    # single-key openai).
    monkeypatch.delenv("HELLO_SERVICE_API_KEY", raising=False)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = make_studio_repo(tmp_path, projects=("hello",))
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(repo / "catalog"))
    return repo


def _seed_session_index(tmp_path: Path) -> None:
    index_path = tmp_path / "foundry_home" / "studio" / "chat_sessions.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "s_STORED01": {
                    "project": "hello",
                    "created_at": "2026-07-15T10:00:00+00:00",
                    "run_ids": ["01KXSTOREDRUN01"],
                    "multi_turn": False,
                },
                "s_OTHERPROJ": {
                    "project": "elsewhere",
                    "created_at": "2026-07-15T11:00:00+00:00",
                    "run_ids": [],
                    "multi_turn": False,
                },
            }
        )
    )


async def _lifespan_client(app: Any) -> Any:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://studio"
    )


async def test_run_shaped_route_returns_424_envelope(repo: Path) -> None:
    app = create_studio_app(repo, serve_assets=False)
    async with app.router.lifespan_context(app):
        async with await _lifespan_client(app) as client:
            opened = await client.post("/api/chat/hello/sessions")
            assert opened.status_code == 424
            body = opened.json()
            assert body["error_class"] == "ProjectUnavailableError"
            assert body["context"]["project"] == "hello"
            assert body["context"]["env_vars"] == ["HELLO_SERVICE_API_KEY"]
            assert "HELLO_SERVICE_API_KEY" in body["context"]["remedy"]
            assert "credentials_ref" in body["context"]["remedy"]
            # The original missing-env ConfigLoadError rides the chain.
            assert any(
                c["error_class"] == "ConfigLoadError"
                for c in body["cause_chain"]
            )


async def test_sessions_list_returns_stored_sessions_without_compiling(
    repo: Path, tmp_path: Path
) -> None:
    _seed_session_index(tmp_path)
    app = create_studio_app(repo, serve_assets=False)
    async with app.router.lifespan_context(app):
        async with await _lifespan_client(app) as client:
            listed = await client.get("/api/chat/hello/sessions")
            assert listed.status_code == 200
            sessions = listed.json()
            assert [s["session_id"] for s in sessions] == ["s_STORED01"]
            assert sessions[0]["run_ids"] == ["01KXSTOREDRUN01"]
            # Schema unknown without a compile — the UI falls back to the
            # plain composer (which it disables via the banner anyway).
            assert sessions[0]["input_fields"] == []

            # Unknown projects still 404 (not 424, not an empty list).
            missing = await client.get("/api/chat/nope/sessions")
            assert missing.status_code == 404


async def test_project_detail_exposes_unavailable_block(repo: Path) -> None:
    app = create_studio_app(repo, serve_assets=False)
    async with app.router.lifespan_context(app):
        async with await _lifespan_client(app) as client:
            detail = await client.get("/api/projects/hello")
            assert detail.status_code == 200
            block = detail.json()["unavailable"]
            assert block is not None
            assert block["env_vars"] == ["HELLO_SERVICE_API_KEY"]
            assert "restart foundry studio" in block["remedy"]


async def test_detail_block_clears_once_env_var_is_set(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    app = create_studio_app(repo, serve_assets=False)
    async with app.router.lifespan_context(app):
        async with await _lifespan_client(app) as client:
            detail = await client.get("/api/projects/hello")
            assert detail.status_code == 200
            assert detail.json()["unavailable"] is None
            sessions = await client.get("/api/chat/hello/sessions")
            assert sessions.status_code == 200

            # A compilable project's sessions carry the composer schema.
            opened = await client.post("/api/chat/hello/sessions")
            assert opened.status_code == 201
            assert opened.json()["input_fields"] == [
                {"name": "name", "type": "string", "required": True}
            ]
