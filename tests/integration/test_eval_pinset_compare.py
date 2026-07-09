"""Phase 4 exit-gate integration test: end-to-end comparison across two
pin-sets (docs/03 gate 3) — the same project eval run against the project
as committed at two git refs, with per-agent deltas.

The scenario: a temp git repo holds projects/hello with the agent's prompt
pinned to v1 (HEAD~1) then repinned to v2 (HEAD). The scripted model fake
greets BY NAME only when it sees the v2 prompt (which mentions the
get_time tool), so the two pin sets score differently on the same eval.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from foundry.cli.eval import execute_eval

REPO_ROOT = Path(__file__).resolve().parents[2]
HELLO_DIR = REPO_ROOT / "projects" / "hello"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com",
         "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        timeout=30,
    )


def _pin_prompt(project: Path, version: str) -> None:
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    text = agent_yaml.read_text()
    other = "v1" if version == "v2" else "v2"
    text = text.replace(f"version: {other}\n  path: prompts/{other}.md",
                        f"version: {version}\n  path: prompts/{version}.md")
    agent_yaml.write_text(text)


@pytest.fixture
def pinned_repo(tmp_path: Path) -> Path:
    """A git repo whose HEAD~1 pins hello_agent's prompt to v1 and whose
    HEAD pins it to v2."""
    repo = tmp_path / "repo"
    project = repo / "projects" / "hello"
    project.parent.mkdir(parents=True)
    shutil.copytree(
        HELLO_DIR, project,
        ignore=shutil.ignore_patterns("__pycache__", ".foundry"),
    )
    _git(repo, "init", "-q")
    _pin_prompt(project, "v1")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "pin prompt v1")
    _pin_prompt(project, "v2")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "pin prompt v2")
    return repo


def _prompt_sensitive_transport() -> httpx.MockTransport:
    """Greets by name ONLY under the v2 prompt — discriminated by the
    time-endpoint path that appears in prompts/v2.md and nowhere else (the
    tool DESCRIPTION mentions get_time under both pins, the prompt body
    does not)."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.anthropic.com"
        body = json.loads(request.content)
        system_text = body.get("system", "")
        user_text = body["messages"][0]["content"][0]["text"]
        name = json.loads(user_text)["name"]
        greeting = (
            f"Hello, {name}! It is a fine hour."
            if "api/timezone" in system_text
            else "Hello there, stranger!"
        )
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text",
                     "text": json.dumps({"greeting": greeting})}
                ],
                "stop_reason": "end_turn",
                "model": "claude-haiku-4-5",
                "usage": {"input_tokens": 50, "output_tokens": 20},
            },
        )

    return httpx.MockTransport(handler)


@pytest.mark.integration
def test_pin_set_comparison_reports_per_agent_deltas(
    tmp_path: Path, pinned_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = pinned_repo / "projects" / "hello"
    code = execute_eval(
        "compare",
        [],
        project=str(project),
        pin_sets=["HEAD~1", "HEAD"],
        eval_option=str(project / "evals" / "greeting.yaml"),
        transport=_prompt_sensitive_transport(),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "HEAD~1" in out and "HEAD" in out
    assert "Per-agent breakdown:" in out
    assert "hello_agent" in out
    assert "fail->pass" in out  # all five cases flip under the v2 pin

    # the comparison artifact carries per-case deltas + per-agent scores
    runs_root = tmp_path / "foundry_home" / "runs"
    comparison_files = [
        d / "eval_comparison.json"
        for d in runs_root.iterdir()
        if (d / "eval_comparison.json").exists()
    ]
    assert len(comparison_files) == 1
    comparison = json.loads(comparison_files[0].read_text())
    assert comparison["labels"] == ["HEAD~1", "HEAD"]
    assert comparison["summary"]["score_a"] == 0.0
    assert comparison["summary"]["score_b"] == 1.0
    assert comparison["summary"]["fixes"] == 5
    assert comparison["summary"]["per_agent"] == {"hello_agent": [0.0, 1.0]}
    # one spec hash across both pin sets (docs/40 invariant 5)
    assert {run["eval_spec_hash"] for run in comparison["runs"]} == {
        comparison["eval_spec_hash"]
    }
    # both runs' own artifacts persisted and readable
    result_dirs = [
        d for d in runs_root.iterdir() if (d / "eval_result.json").exists()
    ]
    assert len(result_dirs) == 2
    refs = {
        json.loads((d / "eval_result.json").read_text())["metadata"][
            "pin_set_ref"
        ]
        for d in result_dirs
    }
    assert refs == {"HEAD~1", "HEAD"}
    # the WORKING TREE was never touched (read-only overlay)
    status = subprocess.run(
        ["git", "-C", str(pinned_repo), "status", "--porcelain"],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout.strip()
    assert status == ""


@pytest.mark.integration
def test_worktree_ref_compares_the_live_tree(
    tmp_path: Path, pinned_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = pinned_repo / "projects" / "hello"
    code = execute_eval(
        "compare",
        [],
        project=str(project),
        pin_sets=["HEAD~1", "worktree"],  # live tree == the v2 pin
        eval_option=str(project / "evals" / "greeting.yaml"),
        transport=_prompt_sensitive_transport(),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "worktree" in out


@pytest.mark.integration
def test_unknown_pin_set_ref_is_structured_exit_2(
    pinned_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = pinned_repo / "projects" / "hello"
    code = execute_eval(
        "compare",
        [],
        project=str(project),
        pin_sets=["HEAD", "no-such-ref"],
        eval_option=str(project / "evals" / "greeting.yaml"),
        transport=_prompt_sensitive_transport(),
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "VersioningError" in err
