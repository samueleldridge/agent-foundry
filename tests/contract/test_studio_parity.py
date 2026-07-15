"""Studio CLI-parity + OpenAPI contract tests (docs/72 § The control-plane
API surface; docs/03 § Phase 10a exit gate items 1-2).

The parity rule: EVERY CLI feature in ``foundry.cli.__main__`` has a
corresponding control-plane route. The mapping below encodes the
normative table from docs/72 — a new CLI command without a route (or a
mapping) fails this test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.studio.app import create_studio_app

REPO_ROOT = Path(__file__).resolve().parents[2]

# CLI command → the route(s) (method, path template) that cover it.
# Four commands map non-1:1 BY DESIGN (docs/72 § API surface):
#   run    → chat (each message = one run) + the runs routes
#   serve  → the chat RunManager pool (studio is not a deployment surface)
#   review → the versions/diff/rollback routes (the TUI's web successor)
#   studio → /api/health (trivially)
PARITY: dict[str, list[tuple[str, str]]] = {
    "run": [
        ("POST", "/api/chat/{project}/sessions/{session_id}/messages"),
        ("GET", "/api/runs/{run_id}"),
        ("GET", "/api/runs/{run_id}/events"),
        ("GET", "/api/runs/{run_id}/artifact"),
    ],
    "resume": [("POST", "/api/runs/{run_id}/resume")],
    "approvals list": [("GET", "/api/approvals")],
    "connections health": [
        ("POST", "/api/projects/{name}/connections/{conn}/health"),
        ("GET", "/api/projects/{name}/connections"),
        ("GET", "/api/projects/{name}/connections/{conn}"),
    ],
    "forge": [
        ("POST", "/api/forge"),
        ("GET", "/api/forge"),
        ("GET", "/api/forge/{forge_run_id}"),
        ("GET", "/api/forge/{forge_run_id}/events"),
        ("POST", "/api/forge/{forge_run_id}/cancel"),
    ],
    "project new": [("POST", "/api/projects")],
    "catalog promote": [("POST", "/api/catalog/promote")],
    "catalog list": [("GET", "/api/catalog")],
    "catalog show": [
        ("GET", "/api/catalog/{kind}/{name}"),
        ("GET", "/api/catalog/{kind}/{name}/{version}/files"),
    ],
    "eval": [
        ("POST", "/api/evals"),
        ("GET", "/api/evals"),
        ("GET", "/api/evals/{eval_run_id}"),
        ("POST", "/api/evals/compare"),
    ],
    "rollback": [("POST", "/api/projects/{name}/rollback")],
    "versions": [("GET", "/api/projects/{name}/versions")],
    "diff": [("GET", "/api/projects/{name}/diff")],
    "serve": [
        ("POST", "/api/chat/{project}/sessions"),
        ("GET", "/api/chat/{project}/sessions"),
        ("GET", "/api/chat/{project}/sessions/{session_id}/events"),
        ("POST", "/api/chat/{project}/sessions/{session_id}/approvals"),
    ],
    "obs cost": [("GET", "/api/obs/cost")],
    "obs tool-failures": [("GET", "/api/obs/tool-failures")],
    "obs p95": [("GET", "/api/obs/latency")],
    "obs runs": [("GET", "/api/obs/runs")],
    "obs eval-trend": [("GET", "/api/obs/eval-trend")],
    "storage stats": [("GET", "/api/storage/stats")],
    "storage gc": [("POST", "/api/storage/gc")],
    "storage archive": [("POST", "/api/storage/archive")],
    "storage pin": [("POST", "/api/storage/pins")],
    "storage unpin": [("DELETE", "/api/storage/pins")],
    "storage list-pinned": [("GET", "/api/storage/pins")],
    "test": [
        ("POST", "/api/projects/{name}/test"),
        ("GET", "/api/tasks/{task_id}"),
        ("GET", "/api/tasks/{task_id}/events"),
    ],
    "doctor": [("GET", "/api/doctor")],
    "review": [
        ("GET", "/api/projects/{name}/versions"),
        ("GET", "/api/projects/{name}/diff"),
        ("POST", "/api/projects/{name}/rollback"),
    ],
    "compute-version": [
        ("GET", "/api/projects/{name}/compute-version"),
    ],
    "deploy": [("POST", "/api/projects/{name}/deploy")],
    "studio": [("GET", "/api/health")],
}

# Routes that exist WITHOUT a CLI counterpart (the studio-only surface —
# legal: parity is CLI → route, not route → CLI).
_STUDIO_ONLY_OK = True


def _cli_commands() -> list[str]:
    """Every registered command in foundry.cli.__main__, sub-typers
    included, exactly as an operator would type it."""
    from foundry.cli.__main__ import app

    def command_name(command: object) -> str:
        name = getattr(command, "name", None)
        if name:
            return str(name)
        callback = getattr(command, "callback", None)
        raw = getattr(callback, "__name__", "")
        return raw.rstrip("_").replace("_", "-")

    names: list[str] = []
    for command in app.registered_commands:
        names.append(command_name(command))
    for group in app.registered_groups:
        instance = group.typer_instance
        assert instance is not None
        group_name = (
            group.name
            or (instance.info.name if instance.info.name else "")
        )
        for command in instance.registered_commands:
            names.append(f"{group_name} {command_name(command)}")
    return sorted(names)


def _studio_routes() -> set[tuple[str, str]]:
    app = create_studio_app(REPO_ROOT, serve_assets=False)
    table = app.state.api_route_table
    assert table, "the app factory recorded no routes"
    return set(table)


@pytest.mark.contract
def test_every_cli_command_has_a_parity_mapping() -> None:
    """A new CLI command without a studio-route mapping fails CI."""
    commands = _cli_commands()
    unmapped = [name for name in commands if name not in PARITY]
    assert unmapped == [], (
        f"CLI command(s) with no studio route mapping: {unmapped} — "
        "add the route(s) and extend PARITY (docs/72 § API surface)"
    )


@pytest.mark.contract
def test_every_mapped_route_exists() -> None:
    """The mapping table may not point at routes that don't exist."""
    routes = _studio_routes()
    missing = [
        (command, method, path)
        for command, pairs in PARITY.items()
        for method, path in pairs
        if (method, path) not in routes
    ]
    assert missing == [], f"parity table names missing routes: {missing}"


@pytest.mark.contract
def test_openapi_schema_covers_every_api_route() -> None:
    """GET /api/openapi.json serves a valid schema covering every route
    (exit-gate item 2)."""
    from starlette.testclient import TestClient

    app = create_studio_app(REPO_ROOT, serve_assets=False)
    client = TestClient(app)
    schema = client.get("/api/openapi.json").json()
    assert schema["info"]["title"] == "foundry studio"
    documented = {
        (method.upper(), path)
        for path, methods in schema["paths"].items()
        for method in methods
    }
    for method, path in _studio_routes():
        if method == "HEAD":
            continue
        # Starlette path converters ({path:path}) render without the
        # converter suffix in OpenAPI.
        normalized = path.replace(":path}", "}")
        assert (method, normalized) in documented, (
            f"route {method} {path} missing from /api/openapi.json"
        )
    # And the normative parity routes are all documented too.
    for pairs in PARITY.values():
        for method, path in pairs:
            assert (method, path) in documented
