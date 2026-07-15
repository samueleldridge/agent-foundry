"""Phase 10a exit-gate: config validation round-trip, commit-on-save,
sandbox refusals, rollback via API, versions/diff (docs/03 § Phase 10a).

Everything runs against THROWAWAY temp git repos — never the real
workspace.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from studio_helpers import REPO_ROOT, git, make_studio_repo

from foundry.config.loader import load_agent_spec
from foundry.core.errors import ConfigValidationError
from foundry.studio.app import create_studio_app

# `state_visibilty` is a deliberate typo: the loader rejects it as
# extra_forbidden with a did-you-mean hint (docs/12 § structured errors).
BAD_AGENT_YAML = """\
name: hello_agent
model_binding:
  provider: anthropic
  model: claude-haiku-4-5
prompt:
  version: v2
  path: prompts/v2.md
output:
  schema: output_schema.py::Greeting
state_visibilty:
  read: [name]
  write: [greeting]
schema_version: 1
"""


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = make_studio_repo(tmp_path)
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(repo / "catalog"))
    return repo


def _client(repo: Path) -> TestClient:
    return TestClient(create_studio_app(repo, serve_assets=False))


# --- validation round-trip -----------------------------------------------------------


@pytest.mark.integration
def test_validate_bad_yaml_returns_pointer_line_column_hint(
    repo: Path,
) -> None:
    """Exit gate: POST .../validate with bad YAML returns structured
    issues with pointer + line + column + hint; message text identical to
    the CLI's (the loader IS the CLI's validator)."""
    with _client(repo) as client:
        response = client.post(
            "/api/projects/hello/validate",
            json={
                "path": "agents/hello_agent/agent.yaml",
                "content": BAD_AGENT_YAML,
            },
        )
    assert response.status_code == 200
    result = response.json()
    assert result["ok"] is False
    assert result["kind"] == "agent"
    issue = result["issues"][0]
    assert issue["severity"] == "error"
    assert issue["pointer"] == "/state_visibilty"
    assert issue["line"] == 11
    assert issue["column"] is not None
    assert issue["hint"] == 'did you mean "state_visibility"?'

    # Message text identical to the loader/CLI error for the same content
    # written to disk (the shadow path is rewritten to the real path).
    target = (
        repo / "projects" / "hello" / "agents" / "hello_agent" / "agent.yaml"
    )
    target.write_text(BAD_AGENT_YAML)
    with pytest.raises(ConfigValidationError) as excinfo:
        load_agent_spec(target)
    assert issue["message"] == str(excinfo.value)


@pytest.mark.integration
def test_validate_python_and_prompt_kinds(repo: Path) -> None:
    with _client(repo) as client:
        bad_py = client.post(
            "/api/projects/hello/validate",
            json={
                "path": "agents/hello_agent/output_schema.py",
                "content": "def broken(:\n",
            },
        ).json()
        assert bad_py["ok"] is False
        assert bad_py["kind"] == "python"
        assert bad_py["issues"][0]["line"] == 1

        empty_prompt = client.post(
            "/api/projects/hello/validate",
            json={
                "path": "agents/hello_agent/prompts/v2.md",
                "content": "   ",
            },
        ).json()
        assert empty_prompt["ok"] is True
        assert empty_prompt["issues"][0]["severity"] == "warning"


# --- write path (commit-on-save) --------------------------------------------------------


@pytest.mark.integration
def test_put_bad_content_writes_nothing(repo: Path) -> None:
    path = "agents/hello_agent/agent.yaml"
    before = (repo / "projects" / "hello" / path).read_text()
    with _client(repo) as client:
        response = client.put(
            f"/api/projects/hello/files/{path}",
            json={"content": BAD_AGENT_YAML},
        )
    assert response.status_code == 422
    assert response.json()["ok"] is False
    assert (repo / "projects" / "hello" / path).read_text() == before
    assert "studio(hello)" not in git(repo, "log", "--oneline", "-3")


@pytest.mark.integration
def test_put_valid_content_commits_and_audits(repo: Path) -> None:
    """Exit gate: PUT validates first, then writes + commits
    `studio(<project>): edit <path>`; commit visible in git log; audit
    entry carries operator.kind = "studio"."""
    path = "agents/hello_agent/agent.yaml"
    with _client(repo) as client:
        loaded = client.get(f"/api/projects/hello/files/{path}").json()
        response = client.put(
            f"/api/projects/hello/files/{path}",
            json={
                "content": loaded["content"] + "\n# studio edit\n",
                "base_hash": loaded["content_hash"],
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["commit_message"] == f"studio(hello): edit {path}"
    log = git(repo, "log", "--oneline", "-1")
    assert body["commit_sha"][:7] in log
    assert f"studio(hello): edit {path}" in log
    changed = git(
        repo, "diff", "--name-only", "HEAD~1", "HEAD"
    ).split()
    assert changed == [f"projects/hello/{path}"]
    audit_lines = (
        repo / "projects" / "hello" / ".foundry" / "audit.jsonl"
    ).read_text().splitlines()
    entry = json.loads(audit_lines[-1])
    assert entry["operator"]["kind"] == "studio"
    assert entry["commit_sha"] == body["commit_sha"]


@pytest.mark.integration
def test_put_stale_base_hash_conflicts(repo: Path) -> None:
    path = "agents/hello_agent/agent.yaml"
    with _client(repo) as client:
        loaded = client.get(f"/api/projects/hello/files/{path}").json()
        response = client.put(
            f"/api/projects/hello/files/{path}",
            json={
                "content": loaded["content"] + "\n# edit\n",
                "base_hash": "0" * 16,
            },
        )
    assert response.status_code == 409
    body = response.json()
    assert body["error_class"] == "StaleContent"
    assert body["context"]["server_content"] == loaded["content"]


# --- sandbox ------------------------------------------------------------------------


@pytest.mark.integration
def test_sandbox_refuses_out_of_project_writes(repo: Path) -> None:
    """Exit gate: writes targeting src/foundry/, catalog/, traversal, and
    symlink escapes → 403 SandboxViolation + studio.sandbox_refused
    event."""
    from foundry.observability.events import get_store

    escape = repo / "outside.txt"
    link = repo / "projects" / "hello" / "escape_link"
    link.symlink_to(repo)
    cases = [
        "..%2F..%2F..%2Fsrc%2Ffoundry%2Fevil.py",  # encoded traversal
        "..%2F..%2Fcatalog%2Ftools%2Fevil.yaml",  # catalog escape
        "escape_link/outside.txt",  # symlink escape
        "evals/greeting.yaml",  # denied subtree
        ".foundry/audit.jsonl",  # denied subtree
    ]
    with _client(repo) as client:
        for case in cases:
            response = client.put(
                f"/api/projects/hello/files/{case}",
                json={"content": "tampered: true"},
            )
            assert response.status_code == 403, case
            assert response.json()["error_class"] == "SandboxViolation"
    assert not escape.exists()
    assert not (REPO_ROOT / "src" / "foundry" / "evil.py").exists()
    refused = [
        row
        for row in get_store().studio_events(project="hello")
        if row["event"] == "studio.sandbox_refused"
    ]
    assert len(refused) == len(cases)


# --- rollback via API ---------------------------------------------------------------


@pytest.mark.integration
def test_rollback_dry_run_default_then_confirmed_apply(repo: Path) -> None:
    """Exit gate: dry-run returns the plan + pre-flight results; the
    confirmed per-prompt rollback produces the same single-file commit as
    the CLI."""
    with _client(repo) as client:
        dry = client.post(
            "/api/projects/hello/rollback",
            json={"prompt": "hello_agent", "to": "v1"},
        )
        assert dry.status_code == 200
        plan = dry.json()
        assert plan["dry_run"] is True
        assert plan["commit_sha"] is None
        assert {check["name"] for check in plan["checks"]} >= {
            "working_tree_clean",
            "target_exists",
        }
        assert "v1" in plan["plan"]
        # dry-run changed nothing
        assert "rollback" not in git(repo, "log", "--oneline", "-1")

        applied = client.post(
            "/api/projects/hello/rollback",
            json={"prompt": "hello_agent", "to": "v1", "dry_run": False},
        )
    assert applied.status_code == 200
    body = applied.json()
    assert body["commit_sha"]
    assert body["audit_entry_id"]
    changed = git(repo, "diff", "--name-only", "HEAD~1", "HEAD").split()
    assert changed == ["projects/hello/agents/hello_agent/agent.yaml"]
    audit_lines = (
        repo / "projects" / "hello" / ".foundry" / "audit.jsonl"
    ).read_text().splitlines()
    entry = json.loads(audit_lines[-1])
    assert entry["type"] == "rollback"
    assert entry["operator"]["kind"] == "studio"


# --- versions / diff / graph 422 -----------------------------------------------------


@pytest.mark.integration
def test_versions_and_diff_routes(repo: Path) -> None:
    path = "agents/hello_agent/agent.yaml"
    with _client(repo) as client:
        loaded = client.get(f"/api/projects/hello/files/{path}").json()
        client.put(
            f"/api/projects/hello/files/{path}",
            json={"content": loaded["content"] + "\n# rev2\n"},
        )
        versions = client.get("/api/projects/hello/versions").json()
        assert versions["branch"] == "main"
        assert versions["commits"][0]["subject"] == (
            f"studio(hello): edit {path}"
        )
        prompts = {row["name"]: row for row in versions["prompts"]}
        assert prompts["hello_agent"]["pinned"] == "v2"
        assert prompts["hello_agent"]["versions"] == ["v1", "v2"]
        tools = {row["name"]: row for row in versions["tools"]}
        assert tools["get_time"]["ref"] == "catalog/http_get_json"

        diff = client.get(
            "/api/projects/hello/diff",
            params={"ref1": "HEAD~1", "ref2": "HEAD"},
        ).json()
        assert [f["path"] for f in diff["files"]] == [
            f"projects/hello/{path}"
        ]
        assert "+# rev2" in diff["files"][0]["hunks"]


@pytest.mark.integration
def test_graph_422_with_validation_result_when_project_broken(
    repo: Path,
) -> None:
    """Exit gate: non-compiling project → 422 with ValidationResult."""
    target = (
        repo / "projects" / "hello" / "agents" / "hello_agent" / "agent.yaml"
    )
    target.write_text(BAD_AGENT_YAML)
    with _client(repo) as client:
        response = client.get("/api/projects/hello/graph")
    assert response.status_code == 422
    result = response.json()
    assert result["ok"] is False
    assert result["kind"] == "project"
    assert result["issues"][0]["pointer"] == "/state_visibilty"
    assert result["issues"][0]["hint"] == 'did you mean "state_visibility"?'


@pytest.mark.integration
def test_catalog_promote_requires_confirm(repo: Path) -> None:
    """Catalog mutation is human-gated: no confirm → refused, nothing
    committed."""
    with _client(repo) as client:
        response = client.post(
            "/api/catalog/promote",
            json={"target": "hello/tool/anything"},
        )
    assert response.status_code == 400
    assert "confirm" in response.json()["message"]
