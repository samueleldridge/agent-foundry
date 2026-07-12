"""Phase 8 exit-gate: the auto-generated API for projects/hello.

POST /run round-trip, OpenAPI ⇄ SystemSpec shape match, header
propagation, auth enforcement, structured validation errors, health,
config, run status.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from api_helpers import (
    HELLO_DIR,
    REPO_ROOT,
    hello_transport,
    read_artifact_events,
)
from starlette.testclient import TestClient

from foundry.api import BearerTokenAuth, create_app


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


def _client() -> TestClient:
    app = create_app(HELLO_DIR, transport=hello_transport())
    return TestClient(app)


@pytest.mark.integration
def test_post_run_round_trip_produces_a_greeting(tmp_path: Path) -> None:
    """Exit gate: serve hello + POST /run → Greeting, with the docs/70
    response headers and a persisted run artifact."""
    with _client() as client:
        response = client.post("/run", json={"name": "world"})
        assert response.status_code == 200
        assert response.json() == {"greeting": "Hello, world!"}
        run_id = response.headers["X-Foundry-Run-Id"]
        assert len(run_id) == 26
        assert response.headers["X-Foundry-System-Version"]
        assert response.headers["X-Foundry-Pin-Set-Hash"]
        assert ":" in response.headers["X-Foundry-Worker-Id"]
        assert response.headers["X-Request-Id"]

        status = client.get(f"/runs/{run_id}").json()
        assert status["status"] == "completed"
        assert status["project"] == "hello"
        assert status["tokens_used"] == 70
        assert status["events_url"] == f"/runs/{run_id}/events"

    events = read_artifact_events(tmp_path, run_id)
    assert events[0]["event"] == "run.started"
    assert events[-1]["event"] == "run.completed"
    assert [e["sequence"] for e in events] == list(range(len(events)))
    # worker_id threads through every persisted event (docs/85).
    assert all(":" in e["worker_id"] for e in events)


@pytest.mark.integration
def test_openapi_schema_matches_systemspec_shapes() -> None:
    """Exit gate: /openapi.json input shape = the start agent's required
    state reads; output shape = the terminal agent's output schema. No
    hand-written per-project routes — the SAME factory serves team_hello
    with its own shapes."""
    with _client() as client:
        openapi = client.get("/openapi.json").json()
    schemas = openapi["components"]["schemas"]
    hello_input = schemas["HelloInput"]
    assert hello_input["required"] == ["name"]
    assert hello_input["properties"]["name"]["type"] == "string"
    assert hello_input["additionalProperties"] is False
    greeting = schemas["Greeting"]
    assert greeting["required"] == ["greeting"]
    assert greeting["properties"]["greeting"]["type"] == "string"
    run_post = openapi["paths"]["/run"]["post"]
    ref = run_post["requestBody"]["content"]["application/json"]["schema"]
    assert ref == {"$ref": "#/components/schemas/HelloInput"}
    ok_schema = run_post["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    assert ok_schema == {"$ref": "#/components/schemas/Greeting"}
    # The catalogue is complete (docs/70 § Endpoint catalogue).
    for path in ("/run", "/stream", "/batch", "/health", "/config"):
        assert path in openapi["paths"], path
    assert "/runs/{run_id}" in openapi["paths"]
    assert "/runs/{run_id}/events" in openapi["paths"]
    assert "/runs/{run_id}/resume" in openapi["paths"]

    # Same factory, different project → different derived shapes.
    from api_helpers import TEAM_DIR

    team_app = create_app(TEAM_DIR, transport=hello_transport())
    with TestClient(team_app) as team_client:
        team_openapi = team_client.get("/openapi.json").json()
    team_input = team_openapi["components"]["schemas"]["TeamHelloInput"]
    assert sorted(team_input["required"]) == ["audience", "request"]
    team_output_ref = team_openapi["paths"]["/run"]["post"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]["$ref"]
    assert team_output_ref.endswith("FinalSummary") or "final_summary" in str(
        team_openapi["components"]["schemas"][
            team_output_ref.rsplit("/", 1)[-1]
        ]["properties"]
    )


@pytest.mark.integration
def test_invalid_body_is_a_structured_400_naming_the_field() -> None:
    with _client() as client:
        response = client.post("/run", json={})
        assert response.status_code == 400
        body = response.json()
        assert body["error_class"] == "ConfigValidationError"
        assert body["context"]["field"] == "name"
        # extra="forbid": unknown fields refuse too (docs/12 discipline).
        response = client.post(
            "/run", json={"name": "x", "surprise": True}
        )
        assert response.status_code == 400


@pytest.mark.integration
def test_bearer_auth_enforced_on_everything_but_health() -> None:
    app = create_app(
        HELLO_DIR,
        transport=hello_transport(),
        auth_backend=BearerTokenAuth(tokens={"sesame"}),
    )
    with TestClient(app) as client:
        assert client.post("/run", json={"name": "x"}).status_code == 401
        assert client.get("/config").status_code == 401
        wrong = client.post(
            "/run",
            json={"name": "x"},
            headers={"Authorization": "Bearer nope"},
        )
        assert wrong.status_code == 401
        assert wrong.json() == {"error": "invalid token"}
        ok = client.post(
            "/run",
            json={"name": "x"},
            headers={"Authorization": "Bearer sesame"},
        )
        assert ok.status_code == 200
        # Health + the OpenAPI schema stay reachable (docs/70).
        assert client.get("/health").status_code == 200
        assert client.get("/openapi.json").status_code == 200


@pytest.mark.integration
def test_noauth_refuses_in_prod(monkeypatch: pytest.MonkeyPatch) -> None:
    from foundry.api import NoAuth
    from foundry.core.errors import ConfigError

    monkeypatch.setenv("FOUNDRY_ENV", "prod")
    with pytest.raises(ConfigError, match="NoAuth is forbidden"):
        NoAuth()


@pytest.mark.integration
def test_health_and_config_surfaces() -> None:
    with _client() as client:
        health = client.get("/health").json()
        assert health["status"] == "alive"
        assert ":" in health["worker_id"]
        deep = client.get("/health", params={"deep": "true"})
        assert deep.status_code == 200
        assert deep.json()["status"] == "ready"
        assert deep.json()["checkpointer"]["ok"] is True

        config = client.get("/config").json()
        assert config["project"] == "hello"
        assert config["agents"] == ["hello_agent"]
        assert config["flow_pattern"] == "single"
        assert config["tools_pinned"] == {
            "get_time": "catalog/http_get_json@v1"
        }
        assert config["connections"]["time_service"]["ref"] == (
            "catalog/http_service"
        )
        # Redaction: no secrets, no env values, no prompt text.
        dumped = json.dumps(config)
        assert "fake-service-key" not in dumped
        assert "HELLO_SERVICE_API_KEY" not in dumped


@pytest.mark.integration
def test_unknown_run_id_is_404() -> None:
    with _client() as client:
        assert client.get("/runs/01AAAAAAAAAAAAAAAAAAAAAAAA").status_code == 404
        assert (
            client.get("/runs/01AAAAAAAAAAAAAAAAAAAAAAAA/events").status_code
            == 404
        )


@pytest.mark.integration
def test_draining_worker_refuses_new_runs_with_503() -> None:
    with _client() as client:
        client.app.state.manager.worker_state.draining = True  # type: ignore[attr-defined]
        response = client.post("/run", json={"name": "x"})
        assert response.status_code == 503
        assert response.headers["Retry-After"]
        assert "draining" in response.json()["message"]
        deep = client.get("/health", params={"deep": "true"})
        assert deep.status_code == 503
        assert deep.json()["status"] == "draining"
