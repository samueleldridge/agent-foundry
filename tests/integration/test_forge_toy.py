"""Phase 6 exit gate: end-to-end forge on the toy numeric-QA problem.

Scripted meta-agent LLM turns drive REAL meta-tools in a throwaway temp
git repo; the forged project's own LLM is a computed responder whose
correctness depends on the live prompt file — so the improvement
trajectory is produced by real file edits + pin moves + commits + evals.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from forge_helpers import (
    DIGIT_HANDLER_FIXED,
    EVAL_SPEC_YAML,
    PROMPT_WITH_BOTH,
    PROMPT_WITH_DIGIT,
    ForgeTransport,
    bootstrap_turns,
    git,
    make_repo,
    prompt_iteration_turns,
)

from foundry.cli.project import execute_project_new
from foundry.configurator import ForgeGuardrails, ForgeSession, MetaAgent
from foundry.versioning import read_audit_entries

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    # meta-agent binds openai/gpt-5-mini; the toy project stays anthropic
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-tests")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTOR", raising=False)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = make_repo(tmp_path)
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(repo / "catalog"))
    return repo


def _read_jsonl(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


async def test_forge_bootstraps_and_improves_to_threshold(
    repo: Path, tmp_path: Path
) -> None:
    # 1. `foundry project new` creates the skeleton + branch (CLI gate).
    code = execute_project_new("qa_bot", projects_root=repo / "projects")
    assert code == 0
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == (
        "foundry/qa_bot"
    )
    project_dir = repo / "projects" / "qa_bot"
    assert (project_dir / "evals").is_dir()
    assert not (project_dir / "system.yaml").exists()  # bootstrap-able

    # 2. The operator supplies the eval set (the meta-agent never writes it).
    (project_dir / "evals" / "qa.yaml").write_text(EVAL_SPEC_YAML)
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "chore(qa_bot): eval set")

    # 3. Scripted forge: bootstrap (0.5) → digit rule (0.833) → reverse
    #    rule (1.0 ≥ threshold 0.9).
    transport = ForgeTransport(
        [
            *bootstrap_turns(),
            *prompt_iteration_turns(
                new_version="v2",
                content=PROMPT_WITH_DIGIT,
                cluster_id="digit_questions_scorer_answer_match",
                summary="prompt v1 -> v2: explicit digitsum tool rule",
                eval_before=0.5,
            ),
            *prompt_iteration_turns(
                new_version="v3",
                content=PROMPT_WITH_BOTH,
                cluster_id="reverse_questions_scorer_answer_match",
                summary="prompt v2 -> v3: explicit reverse rule",
                eval_before=0.833,
            ),
        ]
    )
    agent = MetaAgent(
        "qa_bot",
        projects_root=repo / "projects",
        guardrails=ForgeGuardrails(max_iter=5),
        transport=transport.build(),
    )
    session = ForgeSession(
        meta_agent=agent,
        description="Answer numeric questions: word counts, digit sums, "
        "reversals. Prefer catalog tools.",
        eval_spec_path=Path("projects/qa_bot/evals/qa.yaml"),
        threshold=0.9,
    )
    result = await session.run()

    # --- termination + trajectory (exit gate: ≥2 improvement iterations) ---
    assert result.termination_reason == "threshold_met"
    assert result.threshold_met is True
    assert result.bootstrap is True
    assert result.iterations == 2
    scores = [r.eval_score_after for r in result.trajectory]
    assert scores[0] == pytest.approx(0.5)
    assert scores[1] == pytest.approx(5 / 6, abs=1e-3)
    assert scores[2] == pytest.approx(1.0)
    deltas = [r.eval_delta for r in result.trajectory[1:]]
    assert all(d is not None and d > 0 for d in deltas)
    assert transport.meta_index == len(transport.meta_turns)

    # --- each iteration is a distinct commit referencing the artifact ---
    log = git(repo, "log", "--pretty=%H %s", "foundry/qa_bot")
    lines = log.splitlines()
    for record in result.trajectory:
        assert len(record.commit_shas) == 1
    bootstrap_sha = result.trajectory[0].commit_shas[0]
    iter1_sha = result.trajectory[1].commit_shas[0]
    iter2_sha = result.trajectory[2].commit_shas[0]
    assert len({bootstrap_sha, iter1_sha, iter2_sha}) == 3
    assert any(
        line.startswith(iter1_sha) and "forge(qa_bot/agents/qa_agent)" in line
        for line in lines
    )
    full_message = git(repo, "log", "-1", "--pretty=%B", iter1_sha)
    assert f"Iteration: {result.forge_run_id}" in full_message
    assert "Cluster: digit_questions" in full_message

    # --- catalog tool used in the solution (discovery + pinning) ---
    system_yaml = (project_dir / "system.yaml").read_text()
    assert "ref: catalog/word_count" in system_yaml
    # meta traffic is openai chat.completions: assistant tool calls live in
    # the message's tool_calls array (function.name), not content blocks.
    assert any(
        call["function"]["name"] == "list_catalog"
        for body in transport.meta_requests
        for msg in body["messages"]
        for call in msg.get("tool_calls") or []
        if isinstance(call, dict) and call.get("type") == "function"
    )
    # ... and the forged agent actually CALLED it during evals.
    assert any(
        block.get("name") == "word_count"
        for body in transport.project_requests
        for msg in body["messages"]
        if isinstance(msg["content"], list)
        for block in msg["content"]
        if isinstance(block, dict) and block.get("type") == "tool_use"
    ) or any(
        "word_count" in json.dumps(body) for body in transport.project_requests
    )

    # --- local tool scaffolded, standalone eval iterated until green ---
    handler = (
        project_dir / "tools" / "digit_sum" / "v1" / "handler.py"
    ).read_text()
    assert handler == DIGIT_HANDLER_FIXED
    tool_evals = _tool_eval_results(tmp_path)
    assert [r["passed"] for r in tool_evals] == [False, True]

    # --- trajectory artifact (docs/62 shape) ---
    artifact_dir = Path(result.artifact_dir)
    assert (artifact_dir / "meta.json").is_file()
    assert (artifact_dir / "final_summary.md").is_file()
    trajectory = _read_jsonl(artifact_dir / "trajectory.jsonl")
    assert len(trajectory) == 3
    events = _read_jsonl(artifact_dir / "events.jsonl")
    kinds = [e["event"] for e in events]
    assert kinds[0] == "forge.started"
    assert kinds[-1] == "forge.terminated"
    assert kinds.count("forge.iteration_completed") == 3
    # run_id threading: every forge event carries the forge run id.
    assert all(
        e["run_id"] == result.forge_run_id
        for e in events
        if str(e["event"]).startswith("forge.")
    )

    # --- audit: every meta-agent commit has a meta_agent-operator entry ---
    entries = read_audit_entries(project_dir, type="forge")
    assert len(entries) == 3
    assert all(e.operator.kind == "meta_agent" for e in entries)
    assert all(e.operator.forge_run_id == result.forge_run_id for e in entries)

    # --- no secrets anywhere in the trajectory artifact ---
    combined = "".join(
        p.read_text() for p in artifact_dir.iterdir() if p.is_file()
    )
    assert "fake-anthropic-key-for-tests" not in combined


def _tool_eval_results(tmp_path: Path) -> list[dict[str, object]]:
    """Tool-scope eval artifacts under FOUNDRY_HOME, oldest first."""
    runs_root = tmp_path / "foundry_home" / "runs"
    results = []
    for run in sorted(runs_root.iterdir()):
        result_file = run / "eval_result.json"
        if not result_file.is_file():
            continue
        data = json.loads(result_file.read_text())
        if data["scope"] == "tool" and "digit_sum" in data["target_ref"]:
            results.append(data)
    return results
