"""Review TUI programmatic-layer tests (docs/52 § Review TUI; docs/82).

Every test runs against a THROWAWAY temp git repo built under tmp_path —
never against the real workspace (CLAUDE.md invariant). The repo carries a
copy of projects/hello with two commits (initial + a forge-attributed
prompt tweak), a forge audit entry with eval movement, and a small eval
history — enough surface for every ReviewModel method.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from foundry.cli.tui.review import ReviewModel, run_review_loop, screen_text
from foundry.versioning.audit import (
    EvalContext,
    Operator,
    append_audit_entry,
    new_audit_entry,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


def _configure_identity(repo: Path) -> None:
    """Repo-level identity: the PRODUCT code (rollback via GitBackend)
    commits in this repo too, so transient `-c` flags on the helper's own
    commits aren't enough — CI runners have no global/auto identity."""
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")


def _eval_history_line(
    *, run_id: str, scope: str, target_ref: str, score: float, at: str
) -> str:
    import json

    return json.dumps(
        {
            "eval_run_id": run_id,
            "eval_name": "hello_eval",
            "scope": scope,
            "target_ref": target_ref,
            "target_version": "vtest",
            "eval_spec_hash": "hash1234",
            "pin_set_hash": "pins1234",
            "score": score,
            "passed": True,
            "completed_at": at,
        }
    )


@dataclass(frozen=True)
class Scratch:
    repo: Path
    project_dir: Path
    first_sha: str
    head_sha: str
    original_prompt: str


@pytest.fixture
def scratch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Scratch:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("FOUNDRY_PROJECT_BRANCH", raising=False)

    repo = tmp_path / "repo"
    (repo / "projects").mkdir(parents=True)
    shutil.copytree(
        _REPO_ROOT / "projects" / "hello",
        repo / "projects" / "hello",
        ignore=shutil.ignore_patterns("__pycache__", ".foundry"),
    )
    # .foundry/ is runtime state (audit + eval history) — gitignored so
    # appends never dirty the tree (matches the real repo's layout and the
    # rollback pre-flight working_tree_clean check).
    (repo / ".gitignore").write_text(".foundry/\n__pycache__/\n")
    _git(repo, "init", "-q", "-b", "main")
    _configure_identity(repo)
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "feat(hello): initial project")
    first_sha = _git(repo, "rev-parse", "HEAD")

    prompt = repo / "projects/hello/agents/hello_agent/prompts/v2.md"
    original = prompt.read_text()
    prompt.write_text(original + "\nBe extra concise today.\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "forge(hello): tune hello_agent prompt")
    head_sha = _git(repo, "rev-parse", "HEAD")

    # Rollback pre-flight expects foundry/<project> when it exists (docs/52).
    _git(repo, "checkout", "-q", "-b", "foundry/hello")

    project_dir = repo / "projects" / "hello"
    append_audit_entry(
        project_dir,
        new_audit_entry(
            type="forge",
            scope="hello/agents/hello_agent",
            summary="prompt: hello_agent v2 tuned",
            operator=Operator(
                kind="meta_agent",
                forge_run_id="01FORGE00000000000000000000",
                human_supervisor="op@example.com",
            ),
            commit_sha=head_sha,
            files_affected=["projects/hello/agents/hello_agent/prompts/v2.md"],
            eval_context=EvalContext(
                before_score=0.8,
                before_run_id="01BEFORE0000000000000000000",
                after_score=0.9,
                after_run_id="01AFTER00000000000000000000",
            ),
        ),
    )
    history = project_dir / ".foundry" / "eval_history.jsonl"
    history.write_text(
        "\n".join(
            [
                _eval_history_line(
                    run_id="01EVAL1", scope="project", target_ref="hello",
                    score=0.8, at="2026-07-01T00:00:00+00:00",
                ),
                _eval_history_line(
                    run_id="01EVAL2", scope="agent", target_ref="hello_agent",
                    score=0.85, at="2026-07-02T00:00:00+00:00",
                ),
                _eval_history_line(
                    run_id="01EVAL3", scope="project", target_ref="hello",
                    score=0.9, at="2026-07-03T00:00:00+00:00",
                ),
            ]
        )
        + "\n"
    )
    return Scratch(
        repo=repo,
        project_dir=project_dir,
        first_sha=first_sha,
        head_sha=head_sha,
        original_prompt=original,
    )


@pytest.mark.unit
def test_commits_kinds_and_eval_delta(scratch: Scratch) -> None:
    rows = ReviewModel(scratch.project_dir).commits()
    assert [row.sha for row in rows] == [scratch.head_sha, scratch.first_sha]
    assert rows[0].kind == "forge"
    assert rows[0].eval_delta == pytest.approx(0.1)
    assert rows[1].kind == "human"  # no audit entry recorded that commit
    assert rows[1].eval_delta is None
    assert rows[0].short_sha == scratch.head_sha[:8]


@pytest.mark.unit
def test_commit_detail_scopes_diff_and_carries_audit(scratch: Scratch) -> None:
    detail = ReviewModel(scratch.project_dir).commit_detail(scratch.head_sha)
    assert "v2.md" in detail.diff
    assert "Be extra concise today." in detail.diff
    assert detail.eval_before == pytest.approx(0.8)
    assert detail.eval_after == pytest.approx(0.9)
    assert detail.operator is not None and "meta_agent" in detail.operator
    assert detail.summary == "prompt: hello_agent v2 tuned"


@pytest.mark.unit
def test_commit_detail_root_commit_fallback(scratch: Scratch) -> None:
    detail = ReviewModel(scratch.project_dir).commit_detail(scratch.first_sha)
    assert "system.yaml" in detail.diff  # root commit: diff vs empty tree
    assert detail.summary is None and detail.operator is None


@pytest.mark.unit
def test_artifact_versions_lists_pins_and_eval_scores(scratch: Scratch) -> None:
    rows = ReviewModel(scratch.project_dir).artifact_versions()
    by_key = {(row.kind, row.name): row for row in rows}
    prompt = by_key[("prompt", "hello_agent")]
    assert prompt.pinned == "v2"
    assert prompt.eval_score == pytest.approx(0.85)
    tool = by_key[("tool", "get_time")]
    assert tool.ref == "catalog/http_get_json" and tool.pinned == "v1"
    connection = by_key[("connection", "time_service")]
    assert connection.ref == "catalog/http_service" and connection.pinned == "v1"
    project = by_key[("project", "hello")]
    assert project.eval_score == pytest.approx(0.9)  # latest project-scope run


@pytest.mark.unit
def test_eval_trajectory_is_project_scope_oldest_first(scratch: Scratch) -> None:
    rows = ReviewModel(scratch.project_dir).eval_trajectory()
    assert [row.eval_run_id for row in rows] == ["01EVAL1", "01EVAL3"]
    assert [row.score for row in rows] == [
        pytest.approx(0.8),
        pytest.approx(0.9),
    ]


@pytest.mark.unit
def test_screen_text_renders_commits_tab(scratch: Scratch) -> None:
    model = ReviewModel(scratch.project_dir)
    screen = screen_text(model, tab="commits", selected=0)
    assert scratch.head_sha[:8] in screen
    assert "[r] rollback" in screen
    assert "forge" in screen
    # Every tab renders without touching git state.
    for tab in ("evals", "approvals", "connections"):
        assert "[r] rollback" in screen_text(model, tab=tab)


@pytest.mark.unit
def test_rollback_to_reverts_the_prompt(scratch: Scratch) -> None:
    model = ReviewModel(scratch.project_dir)
    new_sha = model.rollback_to(scratch.first_sha, assume_yes=True)
    assert new_sha not in (scratch.first_sha, scratch.head_sha)
    assert _git(scratch.repo, "rev-parse", "HEAD") == new_sha
    prompt = scratch.project_dir / "agents/hello_agent/prompts/v2.md"
    assert prompt.read_text() == scratch.original_prompt
    rows = model.commits()
    assert len(rows) == 3
    assert rows[0].kind == "rollback"  # the executor wrote an audit entry


@pytest.mark.unit
def test_pending_approvals_empty_and_connections_listed(scratch: Scratch) -> None:
    model = ReviewModel(scratch.project_dir)
    assert model.pending_approvals() == []
    connections = model.connections()
    assert connections and connections[0].name == "time_service"


@pytest.mark.unit
def test_run_review_loop_exits_cleanly_on_eof(
    scratch: Scratch,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(scratch.repo)

    def _eof(_prompt: str = "") -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert run_review_loop("hello") == 0
    assert "[r] rollback" in capsys.readouterr().out
