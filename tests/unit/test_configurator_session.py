"""ForgeSession pre-flight + summary rendering + MetaAgent identity +
CLI argument guards (unit-sized; the loop itself is integration-tested).
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from foundry.cli.forge import execute_forge
from foundry.cli.project import execute_project_new
from foundry.configurator import (
    ForgeError,
    ForgeGuardrails,
    ForgeResult,
    ForgeSession,
    IterationRecord,
    MetaAgent,
    render_summary,
)
from foundry.configurator.meta_agent import (
    DEFAULT_META_MODEL_BINDING,
    compute_meta_agent_version,
)
from foundry.providers import ModelBinding

REPO_SRC = Path(__file__).resolve().parents[2] / "src" / "foundry"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = tmp_path / "repo"
    (repo / "projects" / "qa_bot" / "evals").mkdir(parents=True)
    (repo / "catalog").mkdir()
    (repo / ".gitignore").write_text("projects/*/.foundry/\n")
    (repo / "projects" / "qa_bot" / "evals" / "qa.yaml").write_text(
        "name: qa\nscope: project\ntarget: qa_bot\n"
        "cases: [{id: c1, input: {question: q}, expected: {answer: a}}]\n"
        "scorers: [{kind: exact, name: s, config: {field: answer}}]\n"
        "schema_version: 1\n"
    )
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "op@example.com")
    _git(repo, "config", "user.name", "Operator")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "fixture")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(repo / "catalog"))
    return repo


def _agent(repo: Path) -> MetaAgent:
    return MetaAgent(
        "qa_bot",
        projects_root=repo / "projects",
        guardrails=ForgeGuardrails(max_iter=2),
    )


async def test_pre_flight_requires_project_dir(repo: Path) -> None:
    agent = MetaAgent("ghost", projects_root=repo / "projects")
    session = ForgeSession(
        meta_agent=agent,
        description="x",
        eval_spec_path=Path("projects/ghost/evals/qa.yaml"),
    )
    with pytest.raises(ForgeError, match="foundry project new"):
        await session.run()


async def test_pre_flight_refuses_dirty_project_tree(repo: Path) -> None:
    (repo / "projects" / "qa_bot" / "scratch.txt").write_text("wip")
    session = ForgeSession(
        meta_agent=_agent(repo),
        description="x",
        eval_spec_path=Path("projects/qa_bot/evals/qa.yaml"),
    )
    with pytest.raises(ForgeError, match="uncommitted changes"):
        await session.run()


async def test_pre_flight_requires_project_scope_eval(repo: Path) -> None:
    (repo / "projects" / "qa_bot" / "evals" / "tool.yaml").write_text(
        "name: t\nscope: tool\ntarget: local/x@v1\n"
        "cases: [{id: c1, input: {}, expected: {}}]\n"
        "scorers: [{kind: exact, name: s}]\nschema_version: 1\n"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "tool eval")
    session = ForgeSession(
        meta_agent=_agent(repo),
        description="x",
        eval_spec_path=Path("projects/qa_bot/evals/tool.yaml"),
    )
    with pytest.raises(ForgeError, match="project-scope eval"):
        await session.run()


def test_meta_agent_version_content_hashes_model_binding() -> None:
    prompt = "the prompt"
    default = compute_meta_agent_version(DEFAULT_META_MODEL_BINDING, prompt)
    other = compute_meta_agent_version(
        ModelBinding(provider="openai", model="gpt-5"), prompt
    )
    assert default != other
    assert default.startswith("v1+")
    assert default == compute_meta_agent_version(
        DEFAULT_META_MODEL_BINDING, prompt
    )


def test_meta_agent_prompt_renders_placeholders(repo: Path) -> None:
    from foundry.versioning.git_backend import GitBackend

    agent = _agent(repo)
    bound = agent.bind("01TESTFORGERUNID0000000000", GitBackend(repo))
    prompt = bound.compiled.agent.prompt_text
    assert "{{" not in prompt
    assert "qa_bot" in prompt
    assert str(REPO_SRC) in prompt


def test_lazy_top_level_exports() -> None:
    import foundry

    assert foundry.MetaAgent is MetaAgent
    with pytest.raises(AttributeError):
        _ = foundry.NotAThing  # type: ignore[attr-defined]


def test_render_summary_flags_best_effort() -> None:
    now = datetime.now(UTC)
    result = ForgeResult(
        forge_run_id="01TESTFORGERUNID0000000000",
        project="qa_bot",
        started_at=now,
        completed_at=now,
        duration_s=12.0,
        final_score=0.7,
        best_score=0.7,
        threshold=0.9,
        threshold_met=False,
        iterations=3,
        bootstrap=True,
        termination_reason="max_iter",
        termination_detail="best effort",
        trajectory=[
            IterationRecord(
                iteration_number=0,
                kind="bootstrap",
                summary="scaffolded",
                eval_score_after=0.5,
                commit_shas=["a" * 40],
            )
        ],
    )
    text = render_summary(result)
    assert "Best-effort state" in text
    assert "max_iter" in text
    assert "aaaaaaaa" in text  # short sha rendered


# --- CLI guards ------------------------------------------------------------------


def test_forge_cli_rejects_bad_model_string(repo: Path) -> None:
    code = execute_forge(
        str(repo / "projects" / "qa_bot"),
        description="x",
        eval_path="projects/qa_bot/evals/qa.yaml",
        model="not-a-binding",
    )
    assert code == 2


def test_forge_cli_rejects_missing_project(tmp_path: Path) -> None:
    code = execute_forge(
        str(tmp_path / "nope"),
        description="x",
        eval_path="whatever.yaml",
    )
    assert code == 2


def test_forge_cli_rejects_bad_cost_cap(repo: Path) -> None:
    code = execute_forge(
        str(repo / "projects" / "qa_bot"),
        description="x",
        eval_path="projects/qa_bot/evals/qa.yaml",
        max_cost_usd="lots",
    )
    assert code == 2


def test_project_new_rejects_bad_name(repo: Path) -> None:
    assert execute_project_new("Bad Name!", projects_root=repo / "projects") == 2


def test_project_new_refuses_existing(repo: Path) -> None:
    assert execute_project_new("qa_bot", projects_root=repo / "projects") == 1
