"""Phase 6 exit gates: guardrails + rollback, in throwaway temp repos.

- write_file outside the scoped project aborts the forge (sandbox).
- Cost budget halts the forge at the cap.
- Plateau (no_improvement_after) terminates.
- Threshold miss after max-iter exits with a clear best-effort state.
- Forced prompt regression → meta-agent detects via compare_versions →
  reverts the pin via rollback.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from forge_helpers import (
    EVAL_SPEC_PATH,
    PROMPT_BASE,
    PROMPT_WITH_BOTH,
    PROMPT_WITH_DIGIT,
    ForgeTransport,
    git,
    make_repo,
    meta_final,
    meta_tool_turn,
    prompt_iteration_turns,
    write_scaffolded_project,
)

from foundry.configurator import ForgeGuardrails, ForgeSession, MetaAgent
from foundry.versioning import read_audit_entries, read_prompt_pin

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTOR", raising=False)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = make_repo(tmp_path)
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(repo / "catalog"))
    return repo


def _session(
    repo: Path,
    transport: ForgeTransport,
    guardrails: ForgeGuardrails,
    *,
    threshold: float = 0.9,
) -> ForgeSession:
    agent = MetaAgent(
        "qa_bot",
        projects_root=repo / "projects",
        guardrails=guardrails,
        transport=transport.build(),
    )
    return ForgeSession(
        meta_agent=agent,
        description="Numeric QA agent.",
        eval_spec_path=Path(EVAL_SPEC_PATH),
        threshold=threshold,
    )


def _events(result_artifact_dir: str) -> list[dict[str, Any]]:
    path = Path(result_artifact_dir) / "events.jsonl"
    return [
        json.loads(line)
        for line in path.read_text().splitlines()
        if line.strip()
    ]


# --- sandbox: out-of-project write aborts the run ---------------------------------


async def test_sandbox_violation_aborts_forge(repo: Path) -> None:
    write_scaffolded_project(repo)
    evil = repo / "catalog" / "tools" / "word_count" / "v1" / "handler.py"
    transport = ForgeTransport(
        [
            meta_tool_turn(
                ("write_file", {"path": str(evil), "content": "pwned"})
            ),
            # No further scripted turns: the violation cancels the session,
            # so the next LLM round must never fire.
        ]
    )
    session = _session(repo, transport, ForgeGuardrails(max_iter=3))
    result = await session.run()
    assert result.termination_reason == "sandbox_violation"
    assert "outside the scoped project" in result.termination_detail
    assert result.threshold_met is False
    assert "pwned" not in evil.read_text()
    assert transport.meta_index == 1  # aborted immediately after the attempt
    events = _events(result.artifact_dir)
    assert any(e["event"] == "meta_agent.violation" for e in events)
    assert any(
        e["event"] == "forge.terminated"
        and e["reason"] == "sandbox_violation"
        for e in events
    )


# --- cost budget --------------------------------------------------------------------


async def test_cost_budget_halts_forge(repo: Path) -> None:
    write_scaffolded_project(repo)
    transport = ForgeTransport([])  # no meta turn should ever be needed
    guardrails = ForgeGuardrails(
        max_iter=5, max_cost_usd=Decimal("0.0000001")
    )
    session = _session(repo, transport, guardrails)
    result = await session.run()
    assert result.termination_reason == "cost_exhausted"
    assert result.total_cost_usd is not None
    assert transport.meta_index == 0
    events = _events(result.artifact_dir)
    assert any(
        e["event"] == "forge.terminated" and e["reason"] == "cost_exhausted"
        for e in events
    )


# --- plateau -------------------------------------------------------------------------


def _no_change_iteration_turns() -> list[dict[str, Any]]:
    """An iteration that re-runs the eval without changing anything."""
    return [
        meta_tool_turn(
            (
                "run_eval",
                {
                    "scope": "project",
                    "target": "qa_bot",
                    "eval_spec_path": EVAL_SPEC_PATH,
                },
            )
        ),
        meta_final(
            {
                "action": "iteration_complete",
                "summary": "No promising hypothesis; re-ran the eval.",
                "change_kind": "none",
                "applied": False,
            }
        ),
    ]


async def test_plateau_detection_terminates(repo: Path) -> None:
    write_scaffolded_project(repo)  # baseline 0.5, never improving
    transport = ForgeTransport(
        [*_no_change_iteration_turns(), *_no_change_iteration_turns()]
    )
    guardrails = ForgeGuardrails(max_iter=5, no_improvement_after=2)
    session = _session(repo, transport, guardrails)
    result = await session.run()
    assert result.termination_reason == "plateau"
    assert result.iterations == 2
    assert "no improvement" in result.termination_detail
    assert result.final_score == pytest.approx(0.5)


# --- best effort at max_iter ----------------------------------------------------------


async def test_threshold_miss_exits_best_effort_at_max_iter(
    repo: Path,
) -> None:
    write_scaffolded_project(repo)
    transport = ForgeTransport(
        [
            *prompt_iteration_turns(
                new_version="v2",
                content=PROMPT_WITH_DIGIT,
                cluster_id="digit_questions_scorer_answer_match",
                summary="prompt v1 -> v2: digit rule",
                eval_before=0.5,
            )
        ]
    )
    guardrails = ForgeGuardrails(max_iter=1, no_improvement_after=3)
    session = _session(repo, transport, guardrails)
    result = await session.run()
    assert result.termination_reason == "max_iter"
    assert "best effort" in result.termination_detail
    assert result.threshold_met is False
    assert result.iterations == 1
    assert result.final_score == pytest.approx(5 / 6, abs=1e-3)
    assert result.best_score == pytest.approx(5 / 6, abs=1e-3)
    # The user can inspect the commits and continue manually: the
    # improvement iteration IS committed on the project branch.
    log = git(repo, "log", "--pretty=%s", "foundry/qa_bot")
    assert "forge(qa_bot/agents/qa_agent): prompt v1 -> v2: digit rule" in log
    summary = (Path(result.artifact_dir) / "final_summary.md").read_text()
    assert "Best-effort state" in summary


# --- rollback on regression -------------------------------------------------------------


async def test_meta_agent_detects_regression_and_rolls_back(
    repo: Path,
) -> None:
    """Forced prompt regression: v2 DROPS the digit rule (0.833 → 0.5).
    The scripted meta-agent notices via compare_versions (project scope,
    HEAD~1 vs HEAD — both re-evaluated for real) and reverts the pin with
    the rollback meta-tool, then recovers with a good v3."""
    project_dir = write_scaffolded_project(repo, prompt_v1=PROMPT_WITH_DIGIT)
    agent_dir = "projects/qa_bot/agents/qa_agent"
    regression_iteration = [
        meta_tool_turn(("new_prompt_version", {"agent": "qa_agent"})),
        meta_tool_turn(
            (
                "write_file",
                {"path": f"{agent_dir}/prompts/v2.md", "content": PROMPT_BASE},
            )
        ),
        meta_tool_turn(
            (
                "pin_version",
                {
                    "file": "agents/qa_agent/agent.yaml",
                    "key_path": "prompt.version",
                    "new_version": "v2",
                },
            )
        ),
        meta_tool_turn(
            (
                "git_commit",
                {
                    "files": [
                        f"{agent_dir}/agent.yaml",
                        f"{agent_dir}/prompts/v2.md",
                    ],
                    "scope": "qa_bot/agents/qa_agent",
                    "summary": "prompt v1 -> v2: simplify (experiment)",
                    "eval_before": 0.833,
                },
            )
        ),
        meta_tool_turn(
            (
                "run_eval",
                {
                    "scope": "project",
                    "target": "qa_bot",
                    "eval_spec_path": EVAL_SPEC_PATH,
                },
            )
        ),
        meta_tool_turn(
            (
                "compare_versions",
                {
                    "scope": "project",
                    "target": "qa_bot",
                    "refs": ["HEAD~1", "HEAD"],
                    "eval_spec_path": EVAL_SPEC_PATH,
                },
            )
        ),
        meta_tool_turn(
            (
                "rollback",
                {"scope": "prompt", "target": "qa_agent", "to": "v1"},
            )
        ),
        meta_tool_turn(
            (
                "run_eval",
                {
                    "scope": "project",
                    "target": "qa_bot",
                    "eval_spec_path": EVAL_SPEC_PATH,
                },
            )
        ),
        meta_final(
            {
                "action": "iteration_complete",
                "summary": "v2 regressed (compare_versions: -0.33); "
                "rolled the pin back to v1.",
                "change_kind": "rollback",
                "artifact": "agents/qa_agent/agent.yaml",
                "applied": False,
                "rolled_back": True,
                "notes": "simplifying lost the digit rule; try ADDING the "
                "reverse rule instead",
            }
        ),
    ]
    recovery_iteration = prompt_iteration_turns(
        new_version="v3",
        content=PROMPT_WITH_BOTH,
        cluster_id="reverse_questions_scorer_answer_match",
        summary="prompt v1 -> v3: add reverse rule (keep digit rule)",
        eval_before=0.833,
    )
    transport = ForgeTransport([*regression_iteration, *recovery_iteration])
    guardrails = ForgeGuardrails(max_iter=4, no_improvement_after=3)
    session = _session(repo, transport, guardrails)
    result = await session.run()

    assert result.termination_reason == "threshold_met"
    assert result.iterations == 2
    first, second = result.trajectory
    assert first.rolled_back is True
    assert first.eval_score_after == pytest.approx(5 / 6, abs=1e-3)
    assert second.eval_score_after == pytest.approx(1.0)
    # The pin ended on v3 (v2 never survived).
    version, path = read_prompt_pin(project_dir, "qa_agent")
    assert version == "v3" and path == "prompts/v3.md"
    # ForgeRollback event + rollback audit entry with meta_agent operator.
    events = _events(result.artifact_dir)
    rollbacks = [e for e in events if e["event"] == "forge.rollback"]
    assert len(rollbacks) == 1
    assert rollbacks[0]["scope"] == "prompt"
    assert rollbacks[0]["to_version"] == "v1"
    audit = read_audit_entries(project_dir, type="rollback")
    assert len(audit) == 1
    assert audit[0].operator.kind == "meta_agent"
    assert audit[0].operator.forge_run_id == result.forge_run_id
    # The rollback itself is a commit on the branch.
    log = git(repo, "log", "--pretty=%s", "foundry/qa_bot")
    assert "rollback(qa_bot/agents/qa_agent): prompt v2 → v1" in log
