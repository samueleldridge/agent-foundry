"""Scorer unit coverage (docs/40 § Scorers): exact/numeric/rubric semantics,
registry + config validation, user entry-point discovery."""

from __future__ import annotations

from typing import Any

import pytest

from foundry.core.errors import ConfigValidationError
from foundry.eval.schemas import EvalCase, EvalSpec, ScoredCase, ScorerConfig
from foundry.eval.scorers import (
    ExactScorer,
    NumericScorer,
    RubricScorer,
    ScorerServices,
    build_scorers,
    default_registry,
)


def _case(expected: Any, case_id: str = "c1") -> EvalCase:
    return EvalCase(id=case_id, input={}, expected=expected)


def _exact(config: dict[str, Any], name: str = "x") -> ExactScorer:
    return ExactScorer(
        ScorerConfig(kind="exact", name=name, config=config), ScorerServices()
    )


def _numeric(config: dict[str, Any]) -> NumericScorer:
    return NumericScorer(
        ScorerConfig(kind="numeric", name="n", config=config), ScorerServices()
    )


# --- exact ----------------------------------------------------------------------


@pytest.mark.unit
async def test_exact_whole_object_subset_match() -> None:
    scorer = _exact({})
    scored = await scorer.score(
        _case({"words": 4}), {"words": 4, "characters": 21}, {}
    )
    assert scored.score == 1.0 and scored.pass_
    scored = await scorer.score(
        _case({"words": 5}), {"words": 4, "characters": 21}, {}
    )
    assert scored.score == 0.0 and not scored.pass_
    assert scored.metadata["mismatched_keys"] == ["words"]


@pytest.mark.unit
async def test_exact_field_path_and_expected_slice() -> None:
    scorer = _exact({"field": "result.kind"})
    actual = {"result": {"kind": "auto_resolved"}}
    # expected dict carrying the same path -> compared at the path
    scored = await scorer.score(
        _case({"result": {"kind": "auto_resolved"}}), actual, {}
    )
    assert scored.score == 1.0
    # expected as a bare value -> compared directly
    scored = await scorer.score(_case("auto_resolved"), actual, {})
    assert scored.score == 1.0
    # missing field in actual -> 0.0 with a reason
    scored = await scorer.score(_case("x"), {"result": {}}, {})
    assert scored.score == 0.0
    assert "missing" in scored.metadata["error"]


@pytest.mark.unit
async def test_exact_string_options() -> None:
    insensitive = _exact({"field": "g", "case_sensitive": False, "strip": True})
    scored = await insensitive.score(_case({"g": "Hello"}), {"g": "  hello "}, {})
    assert scored.score == 1.0
    sensitive = _exact({"field": "g"})
    scored = await sensitive.score(_case({"g": "Hello"}), {"g": "hello"}, {})
    assert scored.score == 0.0


@pytest.mark.unit
async def test_exact_fuzzy_regex_and_ratio() -> None:
    regex = _exact({"field": "g", "fuzzy": {"kind": "regex"},
                    "case_sensitive": False})
    scored = await regex.score(
        _case({"g": r"\bworld\b"}), {"g": "Hello, World!"}, {}
    )
    assert scored.score == 1.0
    ratio = _exact(
        {"field": "g", "fuzzy": {"kind": "ratio", "min_ratio": 0.8},
         "pass_threshold": 0.8}
    )
    scored = await ratio.score(_case({"g": "greeting"}), {"g": "greetings"}, {})
    assert 0.8 <= scored.score < 1.0 and scored.pass_
    scored = await ratio.score(_case({"g": "greeting"}), {"g": "xyz"}, {})
    assert scored.score == 0.0 and not scored.pass_


# --- numeric ---------------------------------------------------------------------


@pytest.mark.unit
async def test_numeric_ops_and_tolerances() -> None:
    gte = _numeric(
        {"field": "confidence", "op": "gte",
         "target_field": "expected.confidence_min"}
    )
    scored = await gte.score(
        _case({"confidence_min": 0.85}), {"confidence": 0.9}, {}
    )
    assert scored.score == 1.0
    scored = await gte.score(
        _case({"confidence_min": 0.95}), {"confidence": 0.9}, {}
    )
    assert scored.score == 0.0

    eq_tol = _numeric(
        {"field": "v", "op": "eq", "target_value": 100.0,
         "rel_tolerance": 0.01}
    )
    assert (await eq_tol.score(_case(None), {"v": 100.9}, {})).score == 1.0
    assert (await eq_tol.score(_case(None), {"v": 102.0}, {})).score == 0.0

    between = _numeric({"field": "v", "op": "between", "range": [1, 5]})
    assert (await between.score(_case(None), {"v": 3}, {})).score == 1.0
    assert (await between.score(_case(None), {"v": 9}, {})).score == 0.0


@pytest.mark.unit
async def test_numeric_non_numeric_actual_scores_zero() -> None:
    scorer = _numeric({"field": "v", "op": "gte", "target_value": 1})
    scored = await scorer.score(_case(None), {"v": "high"}, {})
    assert scored.score == 0.0
    assert "non-numeric" in scored.metadata["error"]


@pytest.mark.unit
def test_numeric_config_validation_at_build() -> None:
    with pytest.raises(ConfigValidationError, match="exactly one"):
        _numeric({"field": "v", "op": "gte"})
    with pytest.raises(ConfigValidationError, match="between"):
        _numeric({"field": "v", "op": "between"})


# --- rubric -----------------------------------------------------------------------


@pytest.mark.unit
async def test_rubric_aggregates_weighted_criteria() -> None:
    scorer = RubricScorer(
        ScorerConfig(
            kind="rubric",
            name="quality",
            config={
                "criteria": [
                    {"name": "kind", "judge_kind": "exact",
                     "judge_config": {"field": "kind"}, "weight": 3.0},
                    {"name": "confidence", "judge_kind": "numeric",
                     "judge_config": {"field": "confidence", "op": "gte",
                                      "target_value": 0.8},
                     "weight": 1.0},
                ]
            },
        ),
        ScorerServices(),
    )
    case = _case({"kind": "ok", "confidence": None})
    scored = await scorer.score(case, {"kind": "ok", "confidence": 0.5}, {})
    # kind matches (weight 3), confidence floor fails (weight 1) -> 0.75
    assert scored.score == pytest.approx(0.75)
    assert scored.pass_  # rubric default pass_threshold 0.5
    assert scored.metadata["criteria"]["kind"]["score"] == 1.0
    assert scored.metadata["criteria"]["confidence"]["score"] == 0.0


# --- registry + user discovery ------------------------------------------------------


@pytest.mark.unit
def test_registry_rejects_unknown_kind_and_bad_config() -> None:
    registry = default_registry()
    assert registry.kinds() == [
        "exact", "llm_judge", "numeric", "rubric", "user",
    ]
    with pytest.raises(ConfigValidationError, match="invalid config"):
        registry.create(
            ScorerConfig(kind="exact", name="x", config={"nope": 1}),
            ScorerServices(),
        )


@pytest.mark.unit
def test_build_scorers_validates_every_config_up_front() -> None:
    spec = EvalSpec.model_validate(
        {
            "name": "s", "scope": "tool", "target": "t",
            "cases": [{"id": "a", "input": {}, "expected": {}}],
            "scorers": [
                {"kind": "exact", "name": "ok", "weight": 0.5},
                {"kind": "numeric", "name": "broken", "weight": 0.5,
                 "config": {"field": "v", "op": "between"}},
            ],
        }
    )
    with pytest.raises(ConfigValidationError, match="broken"):
        build_scorers(spec, ScorerServices())


class _UpperScorer:
    """Toy user scorer: passes when the actual value is uppercase."""

    name = ""

    async def score(
        self, case: EvalCase, actual: Any, config: dict[str, Any]
    ) -> ScoredCase:
        value = actual.get(config.get("field", "g"), "")
        ok = isinstance(value, str) and value.isupper()
        return ScoredCase(
            case_id=case.id, scorer_name=self.name,
            score=1.0 if ok else 0.0, pass_=ok,
        )


@pytest.mark.unit
async def test_user_scorer_entry_point_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from importlib.metadata import EntryPoint

    real_entry_points = __import__("importlib.metadata", fromlist=["entry_points"]).entry_points

    class _FakeEntryPoint(EntryPoint):
        def load(self) -> Any:  # type: ignore[override]
            return _UpperScorer

    fake = _FakeEntryPoint(
        name="upper_scorer", value="tests:UpperScorer", group="foundry.scorers"
    )

    def fake_entry_points(**kwargs: Any) -> list[EntryPoint]:
        if kwargs.get("group") == "foundry.scorers":
            if kwargs.get("name") in (None, "upper_scorer"):
                return [fake] if kwargs.get("name") != "missing" else []
            return []
        return list(real_entry_points(**kwargs))

    monkeypatch.setattr(
        "foundry.eval.scorers.user.entry_points", fake_entry_points
    )
    registry = default_registry()
    scorer = registry.create(
        ScorerConfig(kind="user", name="upper_scorer", config={"field": "g"}),
        ScorerServices(),
    )
    assert scorer.name == "upper_scorer"
    scored = await scorer.score(_case(None), {"g": "LOUD"}, {"field": "g"})
    assert scored.score == 1.0

    with pytest.raises(ConfigValidationError, match="not found"):
        registry.create(
            ScorerConfig(kind="user", name="missing", config={}),
            ScorerServices(),
        )
