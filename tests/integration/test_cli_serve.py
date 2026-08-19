"""`foundry serve` wiring: pre-flight compile, env handoff to the uvicorn
factory, and argument validation. The live server is the operator's
manual step (docs/_manual_tests/phase_8.md)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from api_helpers import HELLO_DIR, REPO_ROOT

from foundry.cli.serve import execute_serve


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))
    monkeypatch.delenv("FOUNDRY_SERVE_PROJECT", raising=False)
    monkeypatch.delenv("FOUNDRY_ROUTE_PREFIX", raising=False)
    monkeypatch.delenv("FOUNDRY_API_TOKENS", raising=False)


@pytest.mark.integration
def test_serve_hands_project_to_the_uvicorn_factory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(app: str, **kwargs: Any) -> None:
        captured["app"] = app
        captured.update(kwargs)

    import uvicorn

    monkeypatch.setattr(uvicorn, "run", fake_run)
    # Non-loopback bind: legitimate only because a bearer token is set.
    monkeypatch.setenv("FOUNDRY_API_TOKENS", "t0ken-for-tests")
    code = execute_serve(
        HELLO_DIR, host="0.0.0.0", port=9101, workers=3,
        checkpoint="sqlite", route_prefix="/v1",
    )
    assert code == 0
    assert captured["app"] == "foundry.api.app:create_app_from_env"
    assert captured["factory"] is True
    assert captured["host"] == "0.0.0.0"
    assert captured["port"] == 9101
    assert captured["workers"] == 3
    assert os.environ["FOUNDRY_SERVE_PROJECT"] == str(HELLO_DIR.resolve())
    assert os.environ["FOUNDRY_CHECKPOINTER"] == "sqlite"
    assert os.environ["FOUNDRY_ROUTE_PREFIX"] == "/v1"

    # The factory reconstructs a working app from that environment.
    from foundry.api.app import create_app_from_env

    app = create_app_from_env()
    paths = set(app.openapi()["paths"])
    assert "/v1/run" in paths
    assert "/v1/batch" in paths
    assert "/v1/health" in paths


@pytest.mark.integration
def test_serve_refuses_non_loopback_host_without_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Parity with `foundry studio`: a non-loopback bind without
    FOUNDRY_API_TOKENS refuses to start (docs/70 § Authentication)."""
    assert execute_serve(HELLO_DIR, host="0.0.0.0") == 2
    err = capsys.readouterr().err
    assert "refusing to bind non-loopback host" in err
    assert "FOUNDRY_API_TOKENS" in err

    # Empty/whitespace token env is still "no token".
    monkeypatch.setenv("FOUNDRY_API_TOKENS", "   ")
    assert execute_serve(HELLO_DIR, host="192.168.1.10") == 2
    assert "refusing to bind" in capsys.readouterr().err

    # Loopback binds never need a token.
    monkeypatch.setattr(
        __import__("uvicorn"), "run", lambda app, **kw: None
    )
    assert execute_serve(HELLO_DIR, host="127.0.0.1") == 0


@pytest.mark.integration
def test_serve_argument_validation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert execute_serve(HELLO_DIR, checkpoint="postgres") == 2
    assert "--checkpoint must be one of" in capsys.readouterr().err
    assert execute_serve(HELLO_DIR, workers=0) == 2
    assert execute_serve(HELLO_DIR, workers=4, checkpoint="memory") == 2
    assert "requires a persistent checkpointer" in capsys.readouterr().err
    broken = tmp_path / "nope"
    assert execute_serve(broken) == 2
    err = capsys.readouterr().err
    assert "Error" in err or "error" in err
