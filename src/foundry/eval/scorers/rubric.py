"""RubricScorer — multi-criterion structured judgement (docs/40 § rubric).

Each criterion is independently scored by an underlying scorer (exact /
numeric / llm_judge) run on that criterion only; criterion scores aggregate
per their weights. When ``case.expected`` is a dict carrying the criterion's
name, the criterion is scored against that slice of the expectation.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from foundry.eval.schemas import EvalCase, ScoredCase, ScorerConfig
from foundry.eval.scorers._common import (
    Scorer,
    ScorerServices,
    parse_scorer_config,
)
from foundry.eval.scorers.exact import ExactScorer
from foundry.eval.scorers.llm_judge import LLMJudgeScorer
from foundry.eval.scorers.numeric import NumericScorer


class Criterion(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    judge_kind: Literal["exact", "numeric", "llm_judge"]
    judge_config: dict[str, Any] = Field(default_factory=dict)
    """Config for the underlying scorer, applied to this criterion only."""
    weight: float = Field(default=1.0, ge=0.0)


class RubricScorerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    criteria: list[Criterion] = Field(min_length=1)
    pass_threshold: float = Field(default=0.5, ge=0.0, le=1.0)


_SUB_SCORERS: dict[str, Callable[[ScorerConfig, ScorerServices], Scorer]] = {
    "exact": ExactScorer,
    "numeric": NumericScorer,
    "llm_judge": LLMJudgeScorer,
}


class RubricScorer:
    def __init__(self, config: ScorerConfig, services: ScorerServices) -> None:
        self.name = config.name
        self._config = parse_scorer_config(RubricScorerConfig, config)
        # Sub-scorer configs validate at construction — before any case runs.
        self._sub: list[tuple[Criterion, Scorer]] = [
            (
                criterion,
                _SUB_SCORERS[criterion.judge_kind](
                    ScorerConfig(
                        kind=criterion.judge_kind,
                        name=f"{config.name}.{criterion.name}",
                        config=criterion.judge_config,
                    ),
                    services,
                ),
            )
            for criterion in self._config.criteria
        ]

    async def score(
        self, case: EvalCase, actual: Any, config: dict[str, Any]
    ) -> ScoredCase:
        total_weight = sum(criterion.weight for criterion, _ in self._sub)
        weighted = 0.0
        deterministic = True
        breakdown: dict[str, Any] = {}
        for criterion, scorer in self._sub:
            sub_case = case.model_copy(
                update={"expected": _criterion_expected(case.expected, criterion)}
            )
            scored = await scorer.score(sub_case, actual, criterion.judge_config)
            weighted += criterion.weight * scored.score
            deterministic = deterministic and scored.is_deterministic
            breakdown[criterion.name] = {
                "score": scored.score,
                "pass": scored.pass_,
                "weight": criterion.weight,
                **(
                    {"rationale": scored.metadata["rationale"]}
                    if "rationale" in scored.metadata
                    else {}
                ),
            }
        score = weighted / total_weight if total_weight > 0 else 0.0
        return ScoredCase(
            case_id=case.id,
            scorer_name=self.name,
            score=score,
            pass_=score >= self._config.pass_threshold,
            is_deterministic=deterministic,
            metadata={"criteria": breakdown},
        )


def _criterion_expected(expected: Any, criterion: Criterion) -> Any:
    if isinstance(expected, dict) and criterion.name in expected:
        return expected[criterion.name]
    return expected


__all__ = ["Criterion", "RubricScorer", "RubricScorerConfig"]
