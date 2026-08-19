"""Studio project-new flow (docs/72 § Projects; CLI parity with
``foundry project new``):

- ``POST /api/projects`` scaffolds the skeleton on ``foundry/<name>`` AND
  a validated starter eval template at ``evals/<name>.yaml`` (forge
  requires an eval set; the template is the human's to fill in — TODO
  placeholders, exact scorer, project scope).
- The refusals stay actionable: dirty working trees name the uncommitted
  files in the error context.
- Bootstrap skeletons (no system.yaml yet) are visible to the surfaces
  that can use them: ``GET /api/projects?include_bootstrap=true`` and the
  config editor's file routes.
- ``GET /api/health`` reflects the resolved FOUNDRY_FORGE_MAX_ITER
  default for the forge launch form.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from studio_helpers import git, make_studio_repo

from foundry.config import load_eval_spec
from foundry.studio.app import create_studio_app

pytestmark = pytest.mark.integration


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
    return TestClient(
        create_studio_app(repo, serve_assets=False),
        base_url="http://localhost",
    )


def test_post_projects_scaffolds_skeleton_and_starter_eval(
    repo: Path,
) -> None:
    client = _client(repo)
    created = client.post("/api/projects", json={"name": "qa_bot"})
    assert created.status_code == 201, created.text
    body = created.json()
    assert body["name"] == "qa_bot"
    assert body["branch"] == "foundry/qa_bot"
    assert body["eval_path"] == "evals/qa_bot.yaml"
    assert body["eval_repo_path"] == "projects/qa_bot/evals/qa_bot.yaml"
    assert "evals/qa_bot.yaml" in body["files"]

    # Skeleton on its branch; starter eval committed (clean tree after).
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == (
        "foundry/qa_bot"
    )
    assert not git(repo, "status", "--porcelain").strip()
    project_dir = repo / "projects" / "qa_bot"
    assert not (project_dir / "system.yaml").exists()  # bootstrap-able

    # The template is a REAL project-scope EvalSpec with TODO markers.
    eval_path = project_dir / "evals" / "qa_bot.yaml"
    spec = load_eval_spec(eval_path)
    assert spec.scope == "project"
    assert spec.target == "qa_bot"
    assert len(spec.cases) == 3
    assert spec.scorers[0].kind == "exact"
    assert "TODO" in eval_path.read_text()


def test_starter_eval_is_deep_linkable_in_the_config_editor(
    repo: Path,
) -> None:
    """The config-editor routes work on a bootstrap skeleton: file tree
    lists the starter eval, the file content loads human-editable (the
    eval is the operator's artifact — docs/72 § Eval assistant), and
    validation runs."""
    client = _client(repo)
    assert client.post(
        "/api/projects", json={"name": "qa_bot"}
    ).status_code == 201

    tree = client.get("/api/projects/qa_bot/files")
    assert tree.status_code == 200, tree.text
    files = {entry["path"]: entry for entry in tree.json()["files"]}
    eval_entry = files["evals/qa_bot.yaml"]
    assert eval_entry["kind"] == "eval"
    # Human-editable: the operator owns the eval; only the META-AGENT's
    # sandbox refuses evals/ writes (docs/60 § Eval set immutability).
    assert eval_entry["editable"] is True

    content = client.get("/api/projects/qa_bot/files/evals/qa_bot.yaml")
    assert content.status_code == 200, content.text
    assert "TODO" in content.json()["content"]

    validated = client.post(
        "/api/projects/qa_bot/validate",
        json={
            "path": "evals/qa_bot.yaml",
            "content": content.json()["content"],
        },
    )
    assert validated.status_code == 200
    assert validated.json()["ok"] is True
    assert validated.json()["kind"] == "eval"


def test_bootstrap_projects_listed_only_on_request(repo: Path) -> None:
    client = _client(repo)
    assert client.post(
        "/api/projects", json={"name": "qa_bot"}
    ).status_code == 201

    default_names = [
        row["name"] for row in client.get("/api/projects").json()
    ]
    assert "qa_bot" not in default_names  # run-shaped surfaces unaffected

    rows = client.get("/api/projects?include_bootstrap=true").json()
    by_name = {row["name"]: row for row in rows}
    assert by_name["hello"]["bootstrap"] is False
    qa = by_name["qa_bot"]
    assert qa["bootstrap"] is True
    assert qa["agent_count"] == 0
    assert "forge bootstrap" in qa["health_detail"]


def test_dirty_tree_refusal_names_the_uncommitted_files(
    repo: Path,
) -> None:
    (repo / "projects" / "hello" / "scratch.txt").write_text("wip\n")
    client = _client(repo)
    refused = client.post("/api/projects", json={"name": "qa_bot"})
    assert refused.status_code == 400, refused.text
    body = refused.json()
    assert body["error_class"] == "ConfigValidationError"
    assert "uncommitted changes" in body["message"]
    assert body["context"]["dirty_files"] == ["projects/hello/scratch.txt"]
    assert not (repo / "projects" / "qa_bot").exists()


def test_existing_project_refusal_is_structured(repo: Path) -> None:
    client = _client(repo)
    refused = client.post("/api/projects", json={"name": "hello"})
    assert refused.status_code == 400
    assert refused.json()["context"]["exists"] is True


def test_scaffold_eval_false_skips_the_template(repo: Path) -> None:
    client = _client(repo)
    created = client.post(
        "/api/projects", json={"name": "qa_bot", "scaffold_eval": False}
    )
    assert created.status_code == 201
    assert created.json()["eval_path"] is None
    assert not (
        repo / "projects" / "qa_bot" / "evals" / "qa_bot.yaml"
    ).exists()


def test_health_reflects_forge_max_iter_env_default(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _client(repo)
    assert client.get("/api/health").json()["forge_max_iter_default"] == 5
    monkeypatch.setenv("FOUNDRY_FORGE_MAX_ITER", "9")
    assert client.get("/api/health").json()["forge_max_iter_default"] == 9
