"""compare_runs unit coverage (docs/40 § EvalComparison): delta detection,
pass/fail flips, spec-hash guard, per-agent breakdown."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from foundry.core import RunId
from foundry.core.errors import ConfigValidationError
from foundry.eval import compare_runs
from foundry.eval.schemas import CaseResult, EvalRunResult


def _run(
    case_scores: dict[str, tuple[float, bool]],
    *,
    spec_hash: str = "hash-1",
    per_agent: dict[str, float] | None = None,
) -> EvalRunResult:
    per_case = [
        CaseResult(case_id=case_id, score=score, pass_=passed)
        for case_id, (score, passed) in case_scores.items()
    ]
    total = sum(s for s, _ in case_scores.values()) / max(len(case_scores), 1)
    metadata: dict[str, Any] = {}
    if per_agent is not None:
        metadata["per_agent"] = per_agent
    return EvalRunResult(
        eval_run_id=RunId.new(),
        eval_name="e",
        scope="project",
        eval_spec_hash=spec_hash,
        target_ref="hello",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        cases_total=len(per_case),
        score=total,
        per_case=per_case,
        metadata=metadata,
    )


@pytest.mark.unit
def test_deltas_and_flip_detection() -> None:
    """docs/40 unit test 7: same spec hash, flips identified per direction."""
    run_a = _run({
        "stays_passing": (1.0, True),
        "regresses": (1.0, True),
        "gets_fixed": (0.2, False),
    })
    run_b = _run({
        "stays_passing": (1.0, True),
        "regresses": (0.3, False),
        "gets_fixed": (1.0, True),
    })
    comparison = compare_runs([run_a, run_b], ["v1", "v2"])

    by_id = {d.case_id: d for d in comparison.deltas}
    assert not by_id["stays_passing"].flipped
    assert by_id["regresses"].flipped
    assert by_id["regresses"].flip_direction == "regression"
    assert by_id["regresses"].delta == pytest.approx(-0.7)
    assert by_id["gets_fixed"].flip_direction == "fix"
    assert by_id["gets_fixed"].scores == [0.2, 1.0]

    summary = comparison.summary
    assert summary.label_a == "v1" and summary.label_b == "v2"
    assert summary.regressions == 1 and summary.fixes == 1
    assert summary.delta == pytest.approx(run_b.score - run_a.score)


@pytest.mark.unit
def test_three_way_comparison_compares_first_and_last() -> None:
    runs = [
        _run({"c": (0.0, False)}),
        _run({"c": (0.5, False)}),
        _run({"c": (1.0, True)}),
    ]
    comparison = compare_runs(runs, ["v1", "v2", "v3"])
    delta = comparison.deltas[0]
    assert delta.scores == [0.0, 0.5, 1.0]
    assert delta.delta == pytest.approx(1.0)
    assert delta.flip_direction == "fix"
    assert comparison.summary.label_b == "v3"


@pytest.mark.unit
def test_different_spec_hashes_are_refused() -> None:
    """docs/40 invariant 5: compare is always against the same spec hash."""
    run_a = _run({"c": (1.0, True)}, spec_hash="hash-1")
    run_b = _run({"c": (1.0, True)}, spec_hash="hash-2")
    with pytest.raises(ConfigValidationError, match="DIFFERENT eval specs"):
        compare_runs([run_a, run_b], ["a", "b"])


@pytest.mark.unit
def test_per_agent_breakdown_collects_scores_per_run() -> None:
    run_a = _run({"c": (0.8, True)}, per_agent={"hello_agent": 0.8})
    run_b = _run({"c": (0.9, True)}, per_agent={"hello_agent": 0.9})
    comparison = compare_runs([run_a, run_b], ["HEAD~1", "HEAD"])
    assert comparison.summary.per_agent == {"hello_agent": [0.8, 0.9]}


@pytest.mark.unit
def test_fewer_than_two_runs_is_refused() -> None:
    with pytest.raises(ConfigValidationError, match=">= 2"):
        compare_runs([_run({"c": (1.0, True)})], ["only"])
