"""LLMJudgeScorer — rubric-based LLM judgement (docs/40 § llm_judge).

The judge model is a ``ModelBinding`` resolved through the standard
provider registry — ANY registered provider works, nothing is hardcoded
(docs/03 § Phase 4 exit gate). Judge calls run on the eval-scoped session
so their cost counts against the eval budget, and they emit ``llm.started``
/ ``llm.completed`` events like any other LLM call.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from foundry.core import (
    FoundryMessage,
    LLMCallCompleted,
    LLMCallStarted,
    MessageRole,
    ModelResponse,
    TextBlock,
)
from foundry.eval.schemas import EvalCase, ScoredCase, ScorerConfig
from foundry.eval.scorers._common import ScorerServices, parse_scorer_config
from foundry.providers import ModelBinding, resolve

_JUDGE_SYSTEM = (
    "You are an evaluation judge. Score how well the ACTUAL output satisfies "
    "the rubric below, on a scale from 0.0 (completely fails) to 1.0 (fully "
    "satisfies). Respond ONLY with a single JSON object — no code fences, no "
    'commentary — of the shape: {"score": <float 0.0-1.0>, "rationale": '
    '"<one or two sentences>"}'
)


class JudgeOutput(BaseModel):
    """The judge's structured verdict (docs/40: at minimum score + rationale).
    Misshapen judge responses fail validation and surface as scorer errors."""

    model_config = ConfigDict(extra="allow")

    score: float = Field(ge=0.0, le=1.0)
    rationale: str = ""


class LLMJudgeScorerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    judge_model_binding: ModelBinding
    """Provider-agnostic. Recommended: a different vendor than the agent
    under test, to reduce judge/judged co-bias."""
    rubric_template: str
    """Markdown with {input}, {expected}, {actual} placeholders."""
    pass_threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    calibration_set: str | None = None
    """Accepted but not applied in Phase 4 (docs/40 open question 2 —
    judge-bias regression deferred); a set value adds a metadata note."""


class LLMJudgeScorer:
    def __init__(self, config: ScorerConfig, services: ScorerServices) -> None:
        self.name = config.name
        self._config = parse_scorer_config(LLMJudgeScorerConfig, config)
        self._services = services
        # Resolved through the provider registry: unknown provider/model or
        # unresolvable credentials fail HERE, at load, not mid-eval.
        self._provider = resolve(
            self._config.judge_model_binding,
            services.secrets,
            transport=services.transport,
        )
        self._settings = self._config.judge_model_binding.settings
        if services.deterministic:
            update: dict[str, Any] = {"temperature": 0.0}
            if services.seed is not None and self._provider.capabilities.seed:
                update["seed"] = services.seed
            self._settings = self._settings.model_copy(update=update)

    async def score(
        self, case: EvalCase, actual: Any, config: dict[str, Any]
    ) -> ScoredCase:
        rubric = _render(self._config.rubric_template, case, actual)
        messages = [
            FoundryMessage(
                role=MessageRole.SYSTEM, content=[TextBlock(text=_JUDGE_SYSTEM)]
            ),
            FoundryMessage(role=MessageRole.USER, content=[TextBlock(text=rubric)]),
        ]
        emit = self._services.emit
        if emit is not None:
            emit(
                LLMCallStarted,
                agent_name=f"judge:{self.name}",
                provider=self._provider.name,
                model=self._provider.model,
            )
        response = await self._provider.generate(
            messages, [], self._settings, self._services.judge_session
        )
        if emit is not None:
            emit(
                LLMCallCompleted,
                agent_name=f"judge:{self.name}",
                usage=response.usage,
                cost_estimate_usd=response.cost_estimate_usd,
                latency_ms=response.latency_ms,
                stop_reason=response.stop_reason,
            )
        verdict = _parse_verdict(response)
        metadata: dict[str, Any] = {
            "rationale": verdict.rationale,
            "judge_provider": self._provider.name,
            "judge_model": self._provider.model,
            "judge_input_tokens": response.usage.input_tokens,
            "judge_output_tokens": response.usage.output_tokens,
            "judge_cost_usd": (
                str(response.cost_estimate_usd)
                if response.cost_estimate_usd is not None
                else None
            ),
        }
        if self._config.calibration_set is not None:
            metadata["calibration"] = (
                "calibration_set configured but calibration is deferred "
                "(docs/40 open question 2); raw judge score reported"
            )
        return ScoredCase(
            case_id=case.id,
            scorer_name=self.name,
            score=verdict.score,
            pass_=verdict.score >= self._config.pass_threshold,
            is_deterministic=False,
            metadata=metadata,
        )


def _render(template: str, case: EvalCase, actual: Any) -> str:
    """Literal placeholder substitution — NOT str.format, so braces in the
    rubric prose (JSON examples etc.) never explode."""
    return (
        template.replace("{input}", json.dumps(case.input, default=str))
        .replace("{expected}", json.dumps(case.expected, default=str))
        .replace("{actual}", json.dumps(actual, default=str))
    )


def _parse_verdict(response: ModelResponse) -> JudgeOutput:
    text = "".join(
        block.text
        for block in response.message.content
        if isinstance(block, TextBlock)
    ).strip()
    if text.startswith("```"):
        text = text.strip("`\n")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return JudgeOutput.model_validate_json(text)
    except ValidationError as exc:
        raise ValueError(
            f"judge response failed validation against JudgeOutput "
            f"(score: float 0-1, rationale: str): {text[:200]!r}"
        ) from exc


def judge_cost(scored: ScoredCase) -> tuple[int, Decimal | None]:
    """(tokens, cost) a judge verdict added — the harness folds these into
    the case tallies so judge spend is visible per case."""
    tokens = int(scored.metadata.get("judge_input_tokens", 0)) + int(
        scored.metadata.get("judge_output_tokens", 0)
    )
    raw_cost = scored.metadata.get("judge_cost_usd")
    return tokens, Decimal(raw_cost) if raw_cost else None


__all__ = [
    "JudgeOutput",
    "LLMJudgeScorer",
    "LLMJudgeScorerConfig",
    "judge_cost",
]
