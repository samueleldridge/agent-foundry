"""Provider/model panel routes (docs/72 § Provider panel): key storage
precedence (real env ALWAYS wins), 0600 file mode, delete semantics,
faked-transport verification, manifest-backed model listings, and the
compile-cache invalidation that lets a 424-unavailable project recover
after a key save — no studio restart."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient
from studio_helpers import make_studio_repo

from foundry.studio.app import create_studio_app

pytestmark = pytest.mark.integration

PROVIDER_VARS = (
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "VOYAGE_API_KEY",
    "COHERE_API_KEY",
)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Fresh FOUNDRY_HOME + no provider vars leaking in OR out (the key
    routes write os.environ directly, which monkeypatch cannot track)."""
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    before = {var: os.environ.get(var) for var in PROVIDER_VARS}
    for var in PROVIDER_VARS:
        os.environ.pop(var, None)
    yield
    for var, value in before.items():
        if value is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = value


def _client(
    repo: Path, transport: httpx.AsyncBaseTransport | None = None
) -> TestClient:
    return TestClient(
        create_studio_app(repo, transport=transport, serve_assets=False)
    )


def _credentials_file(tmp_path: Path) -> Path:
    return tmp_path / "foundry_home" / "studio" / "credentials.env"


# --- key write / load / precedence ----------------------------------------------------


def test_put_key_stores_0600_loads_env_and_reports_studio_source(
    tmp_path: Path,
) -> None:
    with _client(tmp_path) as client:
        response = client.put(
            "/api/providers/anthropic/key",
            json={"api_key": "fake-anthropic-key-abcd"},
        )
        assert response.status_code == 200
        assert response.json() == {
            "provider": "anthropic",
            "var_name": "ANTHROPIC_API_KEY",
            "set": True,
            "source": "studio",
            "last4": "abcd",
        }
        # Loaded into the process env (projects compile without restart).
        assert os.environ["ANTHROPIC_API_KEY"] == "fake-anthropic-key-abcd"
        # Stored server-side, owner-only.
        path = _credentials_file(tmp_path)
        assert path.read_text() == "ANTHROPIC_API_KEY=fake-anthropic-key-abcd\n"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

        statuses = {
            row["provider"]: row
            for row in client.get("/api/providers/keys").json()
        }
        assert set(statuses) == {"anthropic", "openai", "voyage", "cohere"}
        assert statuses["anthropic"]["source"] == "studio"
        assert statuses["openai"] == {
            "provider": "openai",
            "var_name": "OPENAI_API_KEY",
            "set": False,
            "source": "unset",
            "last4": None,
        }


def test_real_environment_always_wins_over_studio_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.environ["OPENAI_API_KEY"] = "real-env-key"  # fixture restores
    with _client(tmp_path) as client:
        response = client.put(
            "/api/providers/openai/key", json={"api_key": "studio-key-9999"}
        )
        assert response.status_code == 200
        body = response.json()
        # Stored, but shadowed: reported as env-sourced, last4 withheld.
        assert body["set"] is True
        assert body["source"] == "environment"
        assert body["last4"] is None
        assert os.environ["OPENAI_API_KEY"] == "real-env-key"


def test_startup_loads_stored_keys_only_where_env_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.environ["OPENAI_API_KEY"] = "real-env-key"  # fixture restores
    path = _credentials_file(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(
        "ANTHROPIC_API_KEY=stored-anthropic-1234\n"
        "OPENAI_API_KEY=stored-openai-5678\n"
    )
    with _client(tmp_path) as client:
        assert os.environ["ANTHROPIC_API_KEY"] == "stored-anthropic-1234"
        assert os.environ["OPENAI_API_KEY"] == "real-env-key"
        statuses = {
            row["provider"]: row["source"]
            for row in client.get("/api/providers/keys").json()
        }
        assert statuses["anthropic"] == "studio"
        assert statuses["openai"] == "environment"


def test_key_rotation_updates_studio_owned_env_var(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        client.put("/api/providers/cohere/key", json={"api_key": "first-key"})
        assert os.environ["COHERE_API_KEY"] == "first-key"
        response = client.put(
            "/api/providers/cohere/key", json={"api_key": "second-key"}
        )
        assert response.json()["last4"] == "-key"
        assert os.environ["COHERE_API_KEY"] == "second-key"


def test_put_rejects_blank_keys(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        assert (
            client.put(
                "/api/providers/anthropic/key", json={"api_key": ""}
            ).status_code
            == 400
        )
        response = client.put(
            "/api/providers/anthropic/key", json={"api_key": "   "}
        )
        assert response.status_code == 400
        assert response.json()["error_class"] == "ConfigValidationError"
        assert not _credentials_file(tmp_path).exists()


def test_stub_providers_manage_no_keys(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.put(
            "/api/providers/bedrock/key", json={"api_key": "x"}
        )
        assert response.status_code == 400
        assert response.json()["context"]["stub"] is True
        assert (
            client.put(
                "/api/providers/nope/key", json={"api_key": "x"}
            ).status_code
            == 404
        )


# --- delete semantics -------------------------------------------------------------------


def test_delete_removes_studio_stored_key_everywhere(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        client.put("/api/providers/voyage/key", json={"api_key": "fake-key"})
        response = client.delete("/api/providers/voyage/key")
        assert response.status_code == 200
        assert response.json()["set"] is False
        assert response.json()["source"] == "unset"
        assert "VOYAGE_API_KEY" not in os.environ
        assert "VOYAGE_API_KEY" not in _credentials_file(tmp_path).read_text()


def test_delete_refuses_env_sourced_keys_with_structured_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    os.environ["ANTHROPIC_API_KEY"] = "real-env-key"  # fixture restores
    with _client(tmp_path) as client:
        response = client.delete("/api/providers/anthropic/key")
        assert response.status_code == 400
        body = response.json()
        assert body["error_class"] == "ConfigValidationError"
        assert body["context"]["source"] == "environment"
        assert "restart foundry studio" in body["message"]
        assert os.environ["ANTHROPIC_API_KEY"] == "real-env-key"


def test_delete_without_stored_key_is_404(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        response = client.delete("/api/providers/anthropic/key")
        assert response.status_code == 404
        assert response.json()["context"]["not_found"] is True


# --- verify (faked transport) ------------------------------------------------------------


def _verify_transport(
    seen: list[httpx.Request], status: int, body: dict[str, Any]
) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status, json=body)

    return httpx.MockTransport(handler)


def test_verify_ok_round_trips_the_cheapest_call(tmp_path: Path) -> None:
    seen: list[httpx.Request] = []
    transport = _verify_transport(seen, 200, {"data": []})
    with _client(tmp_path, transport=transport) as client:
        client.put(
            "/api/providers/anthropic/key", json={"api_key": "fake-key-abcd"}
        )
        response = client.post("/api/providers/anthropic/key/verify")
        assert response.status_code == 200
        assert response.json() == {
            "provider": "anthropic",
            "var_name": "ANTHROPIC_API_KEY",
            "ok": True,
            "status_code": 200,
            "detail": "credentials accepted",
        }
    (request,) = seen
    assert request.url == "https://api.anthropic.com/v1/models?limit=1"
    assert request.headers["x-api-key"] == "fake-key-abcd"


def test_verify_auth_failure_reports_without_leaking_the_key(
    tmp_path: Path,
) -> None:
    planted = "planted-fake-provider-key-zz99"
    seen: list[httpx.Request] = []
    transport = _verify_transport(
        seen, 401, {"error": {"message": f"bad key {planted}"}}
    )
    with _client(tmp_path, transport=transport) as client:
        client.put("/api/providers/openai/key", json={"api_key": planted})
        response = client.post("/api/providers/openai/key/verify")
        assert response.status_code == 200
        body = response.json()
        assert body["ok"] is False
        assert body["status_code"] == 401
        assert planted not in json.dumps(body)
        assert "rejected the key" in body["detail"]


def test_verify_without_any_key_says_so(tmp_path: Path) -> None:
    with _client(tmp_path) as client:
        body = client.post("/api/providers/cohere/key/verify").json()
        assert body["ok"] is False
        assert body["status_code"] is None
        assert "COHERE_API_KEY is not configured" in body["detail"]


# --- model listings match the shipped manifests -------------------------------------------


def test_provider_models_match_capability_and_pricing_manifests(
    tmp_path: Path,
) -> None:
    from foundry.providers import all_capabilities
    from foundry.providers.embedders.voyage import VOYAGE_MODELS

    with _client(tmp_path) as client:
        providers = {p["name"]: p for p in client.get("/api/providers").json()}

    assert set(providers) == {
        "anthropic", "openai", "voyage", "cohere", "bedrock", "azure", "vertex",
    }
    manifest = {
        caps.model: caps
        for caps in all_capabilities()
        if caps.provider == "anthropic"
    }
    served = {m["id"]: m for m in providers["anthropic"]["models"]}
    assert set(served) == set(manifest)
    for model_id, caps in manifest.items():
        row = served[model_id]
        assert row["context_window"] == caps.max_context_tokens
        assert row["max_output_tokens"] == caps.max_output_tokens
        assert row["reasoning"] == (
            caps.extended_thinking or caps.reasoning_effort
        )
        assert ("tool_use" in row["capabilities"]) == caps.tool_use
        assert row["pricing"]["input_per_1m"] == float(
            caps.pricing.input_per_1m
        )
        assert row["pricing"]["output_per_1m"] == float(
            caps.pricing.output_per_1m
        )

    voyage = providers["voyage"]
    assert voyage["kind"] == "embedder"
    assert {m["id"] for m in voyage["embedding_models"]} == set(VOYAGE_MODELS)
    dims = {
        m["id"]: m["dimensions"] for m in voyage["embedding_models"]
    }
    assert dims == {
        model: caps.dimensions for model, caps in VOYAGE_MODELS.items()
    }

    for stub in ("bedrock", "azure", "vertex"):
        assert providers[stub]["stub"] is True
        assert providers[stub]["note"]
        assert providers[stub]["credentials_env"] is None


# --- compile-cache invalidation: 424 project recovers after a key save --------------------


def test_unavailable_project_recovers_after_key_save_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The operator-report shape (rag_hello / COHERE_API_KEY): chat is 424
    while the provider var is missing; saving the key through the panel
    loads the env + invalidates the compile cache, and the very next
    request compiles."""
    os.environ["ANTHROPIC_API_KEY"] = "fake-anthropic-key-for-tests"
    os.environ["VOYAGE_API_KEY"] = "fake-voyage-key-for-tests"
    repo = make_studio_repo(tmp_path, projects=("rag_hello",))
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(repo / "catalog"))

    with _client(repo) as client:
        opened = client.post("/api/chat/rag_hello/sessions")
        assert opened.status_code == 424
        body = opened.json()
        assert body["error_class"] == "ProjectUnavailableError"
        assert body["context"]["env_vars"] == ["COHERE_API_KEY"]

        saved = client.put(
            "/api/providers/cohere/key",
            json={"api_key": "fake-cohere-key-for-tests"},
        )
        assert saved.status_code == 200

        recovered = client.post("/api/chat/rag_hello/sessions")
        assert recovered.status_code == 201, recovered.text


def test_key_save_drops_previously_compiled_projects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Rotating a key must also drop already-compiled projects (their
    adapters hold the resolved credential)."""
    os.environ["ANTHROPIC_API_KEY"] = "fake-anthropic-key-for-tests"
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    repo = make_studio_repo(tmp_path, projects=("hello",))
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(repo / "catalog"))

    app = create_studio_app(repo, serve_assets=False)
    ctx = app.state.studio_context
    with TestClient(app) as client:
        assert client.post("/api/chat/hello/sessions").status_code == 201
        assert "hello" in ctx._compiled_cache
        client.put(
            "/api/providers/voyage/key", json={"api_key": "fake-key"}
        )
        assert ctx._compiled_cache == {}


def test_key_saves_are_audited_without_the_key_value(tmp_path: Path) -> None:
    from foundry.observability.events import get_store

    with _client(tmp_path) as client:
        client.put(
            "/api/providers/anthropic/key",
            json={"api_key": "super-secret-value-1234"},
        )
        client.delete("/api/providers/anthropic/key")
    rows = get_store().studio_events()
    events = {row["event"]: row for row in rows}
    assert "studio.provider_key_saved" in events
    assert "studio.provider_key_deleted" in events
    for row in rows:
        detail = json.dumps(row)
        assert "super-secret-value-1234" not in detail
        if row["event"].startswith("studio.provider_key"):
            assert json.loads(row["detail"])["operator"] == "studio"
            assert (
                json.loads(row["detail"])["env_var"] == "ANTHROPIC_API_KEY"
            )
