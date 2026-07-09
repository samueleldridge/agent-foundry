"""Failure clustering (docs/41 § Failure categorisation).

Determinism is the load-bearing property: same result → same clusters,
same ids, same order.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from foundry.core import RunId
from foundry.eval import (
    CaseResult,
    EvalRunResult,
    EvalSpec,
    ScoredCase,
    cluster_failures,
)

_RUN_ID = RunId.new()


def _spec(cases: list[dict[str, Any]]) -> EvalSpec:
    return EvalSpec.model_validate(
        {
            "name": "toy",
            "scope": "project",
            "target": "qa_bot",
            "cases": cases,
            "scorers": [{"kind": "exact", "name": "answer_match"}],
            "schema_version": 1,
        }
    )


def _case_result(
    case_id: str, *, passed: bool, score: float = 0.0, scorer: str = "answer_match"
) -> CaseResult:
    return CaseResult.model_validate(
        {
            "case_id": case_id,
            "pass": passed,
            "score": 1.0 if passed else score,
            "scorer_results": [
                ScoredCase.model_validate(
                    {
                        "case_id": case_id,
                        "scorer_name": scorer,
                        "score": 1.0 if passed else score,
                        "pass": passed,
                    }
                )
            ],
        }
    )


def _result(per_case: list[CaseResult]) -> EvalRunResult:
    now = datetime.now(UTC)
    return EvalRunResult(
        eval_run_id=_RUN_ID,
        eval_name="toy",
        scope="project",
        eval_spec_hash="abc",
        target_ref="qa_bot",
        started_at=now,
        completed_at=now,
        per_case=per_case,
    )


def test_clusters_group_by_tags_and_scorer_with_impact_order() -> None:
    spec = _spec(
        [
            {"id": "d1", "input": {}, "expected": {}, "tags": ["digit"]},
            {"id": "d2", "input": {}, "expected": {}, "tags": ["digit"]},
            {"id": "r1", "input": {}, "expected": {}, "tags": ["reverse"]},
            {"id": "w1", "input": {}, "expected": {}, "tags": ["words"]},
        ]
    )
    result = _result(
        [
            _case_result("d1", passed=False),
            _case_result("d2", passed=False),
            _case_result("r1", passed=False),
            _case_result("w1", passed=True),
        ]
    )
    clustering = cluster_failures(spec, result)
    assert len(clustering.clusters) == 2
    top = clustering.clusters[0]
    assert [c.case_id for c in top.cases] == ["d1", "d2"]
    assert "digit" in top.cluster_id
    assert top.impact > clustering.clusters[1].impact
    assert abs(top.impact - 0.5) < 1e-9  # 2 of 4 equally-weighted cases
    assert not clustering.unclustered_failures
    rendered = clustering.render()
    assert top.cluster_id in rendered and "impact" in rendered


def test_clustering_is_deterministic() -> None:
    spec = _spec(
        [
            {"id": f"c{i}", "input": {}, "expected": {}, "tags": ["t"]}
            for i in range(5)
        ]
    )
    result = _result(
        [_case_result(f"c{i}", passed=i % 2 == 0) for i in range(5)]
    )
    first = cluster_failures(spec, result)
    second = cluster_failures(spec, result)
    assert first.model_dump() == second.model_dump()


def test_weights_drive_impact() -> None:
    spec = _spec(
        [
            {
                "id": "heavy",
                "input": {},
                "expected": {},
                "tags": ["a"],
                "weight": 3.0,
            },
            {"id": "light", "input": {}, "expected": {}, "tags": ["b"]},
        ]
    )
    result = _result(
        [
            _case_result("heavy", passed=False),
            _case_result("light", passed=False),
        ]
    )
    clustering = cluster_failures(spec, result)
    assert clustering.clusters[0].cases[0].case_id == "heavy"
    assert abs(clustering.clusters[0].impact - 0.75) < 1e-9


def test_untagged_unscored_failures_are_unclustered() -> None:
    spec = _spec([{"id": "x", "input": {}, "expected": {}}])
    errored = CaseResult.model_validate(
        {"case_id": "x", "status": "error", "pass": False, "score": 0.0}
    )
    clustering = cluster_failures(spec, _result([errored]))
    assert not clustering.clusters
    assert [c.case_id for c in clustering.unclustered_failures] == ["x"]


def test_no_failures_renders_cleanly() -> None:
    spec = _spec([{"id": "x", "input": {}, "expected": {}}])
    clustering = cluster_failures(spec, _result([_case_result("x", passed=True)]))
    assert clustering.render() == "No failures."
