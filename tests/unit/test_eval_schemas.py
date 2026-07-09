"""Eval schemas (docs/40): spec round-trip, hash stability, weight/id
validation, result-shape serialization."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from foundry.config import load_eval_spec
from foundry.core import RunId
from foundry.eval.schemas import (
    CaseResult,
    EvalRunResult,
    EvalSpec,
    ScoredCase,
    eval_spec_hash,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _spec_dict(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "name": "spec",
        "scope": "tool",
        "target": "catalog/word_count@v1",
        "cases": [
            {"id": "a", "input": {"text": "x"}, "expected": {"words": 1}},
            {"id": "b", "input": {"text": ""}, "expected": {"words": 0}},
        ],
        "scorers": [{"kind": "exact", "name": "exact_match"}],
        "schema_version": 1,
    }
    base.update(overrides)
    return base


@pytest.mark.unit
def test_spec_round_trip_and_defaults(tmp_path: Path) -> None:
    spec = EvalSpec.model_validate(_spec_dict())
    # docs/40 defaults
    assert spec.deterministic is True
    assert spec.case_timeout_s == 300.0
    assert spec.replicates == 1
    path = tmp_path / "eval.yaml"
    path.write_text(yaml.safe_dump(json.loads(spec.model_dump_json())))
    reloaded = load_eval_spec(path)
    assert reloaded == spec


@pytest.mark.unit
def test_scorer_weights_must_sum_to_one() -> None:
    with pytest.raises(ValidationError, match=r"sum to 1\.0"):
        EvalSpec.model_validate(
            _spec_dict(
                scorers=[
                    {"kind": "exact", "name": "a", "weight": 0.5},
                    {"kind": "exact", "name": "b", "weight": 0.6},
                ]
            )
        )
    # explicit split that DOES sum
    spec = EvalSpec.model_validate(
        _spec_dict(
            scorers=[
                {"kind": "exact", "name": "a", "weight": 0.4},
                {"kind": "numeric", "name": "b", "weight": 0.6,
                 "config": {"field": "words", "op": "gte", "target_value": 0}},
            ]
        )
    )
    assert [s.weight for s in spec.scorers] == [0.4, 0.6]


@pytest.mark.unit
def test_duplicate_case_ids_rejected() -> None:
    cases = [
        {"id": "dup", "input": {}, "expected": 1},
        {"id": "dup", "input": {}, "expected": 2},
    ]
    with pytest.raises(ValidationError, match="unique"):
        EvalSpec.model_validate(_spec_dict(cases=cases))


@pytest.mark.unit
def test_spec_hash_stable_within_and_across_processes() -> None:
    """docs/40 contract test 3: same spec content -> same hash, in this
    process AND a fresh interpreter."""
    payload = _spec_dict()
    here = eval_spec_hash(EvalSpec.model_validate(payload))
    assert here == eval_spec_hash(EvalSpec.model_validate(payload))

    script = (
        "import json, sys\n"
        "from foundry.eval.schemas import EvalSpec, eval_spec_hash\n"
        "spec = EvalSpec.model_validate(json.loads(sys.argv[1]))\n"
        "print(eval_spec_hash(spec))\n"
    )
    other = subprocess.run(
        [sys.executable, "-c", script, json.dumps(payload)],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
        cwd=REPO_ROOT,
    ).stdout.strip()
    assert other == here


@pytest.mark.unit
def test_spec_hash_changes_with_content() -> None:
    a = eval_spec_hash(EvalSpec.model_validate(_spec_dict()))
    b = eval_spec_hash(EvalSpec.model_validate(_spec_dict(threshold=0.5)))
    assert a != b


@pytest.mark.unit
def test_scored_case_serializes_pass_alias() -> None:
    scored = ScoredCase(case_id="c", scorer_name="s", score=1.0, pass_=True)
    dumped = json.loads(scored.model_dump_json(by_alias=True))
    assert dumped["pass"] is True
    assert ScoredCase.model_validate(dumped).pass_ is True


@pytest.mark.unit
def test_eval_run_result_round_trips_through_json() -> None:
    result = EvalRunResult(
        eval_run_id=RunId.new(),
        eval_name="e",
        scope="project",
        eval_spec_hash="abc",
        target_ref="hello",
        started_at=datetime.now(UTC),
        completed_at=datetime.now(UTC),
        per_case=[
            CaseResult(
                case_id="a",
                score=1.0,
                pass_=True,
                scorer_results=[
                    ScoredCase(
                        case_id="a", scorer_name="s", score=1.0, pass_=True
                    )
                ],
            )
        ],
    )
    dumped = result.model_dump_json(by_alias=True, indent=2)
    reloaded = EvalRunResult.model_validate_json(dumped)
    assert reloaded == result
    assert json.loads(dumped)["per_case"][0]["pass"] is True
