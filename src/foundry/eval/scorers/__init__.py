"""Eval scorers (docs/40 § Scorers): the registry + the four built-ins
(exact, numeric, llm_judge, rubric) + user entry-point discovery.

``default_registry()`` returns the process-wide registry with the built-ins
registered; ``build_scorers`` turns an EvalSpec's scorer configs into live
scorer instances (validating every config before any case runs).
"""

from __future__ import annotations

from foundry.eval.schemas import EvalSpec, ScorerConfig
from foundry.eval.scorers._common import (
    Scorer,
    ScorerFactory,
    ScorerRegistry,
    ScorerServices,
    parse_scorer_config,
    resolve_path,
)
from foundry.eval.scorers.exact import ExactScorer, ExactScorerConfig, FuzzyOptions
from foundry.eval.scorers.llm_judge import (
    JudgeOutput,
    LLMJudgeScorer,
    LLMJudgeScorerConfig,
)
from foundry.eval.scorers.numeric import NumericScorer, NumericScorerConfig
from foundry.eval.scorers.rubric import Criterion, RubricScorer, RubricScorerConfig
from foundry.eval.scorers.user import load_user_scorer

_REGISTRY = ScorerRegistry()
_REGISTRY.register("exact", ExactScorer)
_REGISTRY.register("numeric", NumericScorer)
_REGISTRY.register("llm_judge", LLMJudgeScorer)
_REGISTRY.register("rubric", RubricScorer)
_REGISTRY.register("user", load_user_scorer)


def default_registry() -> ScorerRegistry:
    return _REGISTRY


def build_scorers(
    spec: EvalSpec,
    services: ScorerServices,
    registry: ScorerRegistry | None = None,
) -> list[tuple[ScorerConfig, Scorer]]:
    """Instantiate every scorer the spec declares. Any invalid scorer config,
    unknown kind, missing entry point, or unresolvable judge binding raises
    HERE — at load, before a single case runs (docs/40 failure modes)."""
    registry = registry or _REGISTRY
    return [
        (config, registry.create(config, services)) for config in spec.scorers
    ]


__all__ = [
    "Criterion",
    "ExactScorer",
    "ExactScorerConfig",
    "FuzzyOptions",
    "JudgeOutput",
    "LLMJudgeScorer",
    "LLMJudgeScorerConfig",
    "NumericScorer",
    "NumericScorerConfig",
    "RubricScorer",
    "RubricScorerConfig",
    "Scorer",
    "ScorerFactory",
    "ScorerRegistry",
    "ScorerServices",
    "build_scorers",
    "default_registry",
    "load_user_scorer",
    "parse_scorer_config",
    "resolve_path",
]
