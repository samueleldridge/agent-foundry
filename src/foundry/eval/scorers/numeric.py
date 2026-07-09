"""NumericScorer — numeric comparison with tolerance (docs/40 § numeric).

Useful for confidence floors, latency caps, cost ceilings. Score is 1.0
when the comparison holds, 0.0 otherwise.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from foundry.eval.schemas import EvalCase, ScoredCase, ScorerConfig
from foundry.eval.scorers._common import (
    ScorerServices,
    parse_scorer_config,
    resolve_path,
)


class NumericScorerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str
    """Dotted path into the actual output; must resolve to a number."""
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "between"]
    target_field: str | None = None
    """Dotted path into expected (an 'expected.' prefix is accepted and
    stripped, matching the docs/40 example)."""
    target_value: float | None = None
    abs_tolerance: float | None = Field(default=None, ge=0.0)
    rel_tolerance: float | None = Field(default=None, ge=0.0)
    """For the 'eq' op only."""
    range: tuple[float, float] | None = None
    """For the 'between' op (inclusive bounds)."""
    pass_threshold: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _target_consistency(self) -> NumericScorerConfig:
        if self.op == "between":
            if self.range is None:
                raise ValueError("op 'between' requires `range: [low, high]`")
            if self.range[0] > self.range[1]:
                raise ValueError("range low bound exceeds high bound")
            return self
        given = [
            v for v in (self.target_field, self.target_value) if v is not None
        ]
        if len(given) != 1:
            raise ValueError(
                f"op {self.op!r} requires exactly one of target_field or "
                "target_value"
            )
        if (self.abs_tolerance is not None or self.rel_tolerance is not None) and (
            self.op != "eq"
        ):
            raise ValueError("tolerances apply to the 'eq' op only")
        return self


class NumericScorer:
    def __init__(self, config: ScorerConfig, services: ScorerServices) -> None:
        self.name = config.name
        self._config = parse_scorer_config(NumericScorerConfig, config)

    async def score(
        self, case: EvalCase, actual: Any, config: dict[str, Any]
    ) -> ScoredCase:
        cfg = self._config
        found, raw = resolve_path(actual, cfg.field)
        if not found or not isinstance(raw, int | float) or isinstance(raw, bool):
            return self._scored(
                case, 0.0,
                metadata={
                    "error": f"field {cfg.field!r} missing or non-numeric "
                    f"(got {type(raw).__name__ if found else 'nothing'})"
                },
            )
        value = float(raw)

        if cfg.op == "between":
            assert cfg.range is not None  # validated at load
            low, high = cfg.range
            ok = low <= value <= high
            return self._scored(
                case, 1.0 if ok else 0.0,
                metadata={"value": value, "range": [low, high]},
            )

        target, err = self._target(case.expected, cfg)
        if err is not None:
            return self._scored(case, 0.0, metadata={"error": err})
        assert target is not None
        ok = self._holds(value, target, cfg)
        return self._scored(
            case, 1.0 if ok else 0.0,
            metadata={"value": value, "target": target, "op": cfg.op},
        )

    def _target(
        self, expected: Any, cfg: NumericScorerConfig
    ) -> tuple[float | None, str | None]:
        if cfg.target_value is not None:
            return cfg.target_value, None
        assert cfg.target_field is not None  # validated at load
        path = cfg.target_field.removeprefix("expected.")
        found, raw = resolve_path(expected, path)
        if not found or not isinstance(raw, int | float) or isinstance(raw, bool):
            return None, (
                f"target_field {cfg.target_field!r} missing or non-numeric "
                "in expected"
            )
        return float(raw), None

    def _holds(self, value: float, target: float, cfg: NumericScorerConfig) -> bool:
        if cfg.op == "eq":
            if cfg.abs_tolerance is not None:
                return abs(value - target) <= cfg.abs_tolerance
            if cfg.rel_tolerance is not None:
                return abs(value - target) <= cfg.rel_tolerance * abs(target)
            return value == target
        if cfg.op == "ne":
            return value != target
        if cfg.op == "gt":
            return value > target
        if cfg.op == "gte":
            return value >= target
        if cfg.op == "lt":
            return value < target
        return value <= target  # lte

    def _scored(
        self, case: EvalCase, score: float, *, metadata: dict[str, Any]
    ) -> ScoredCase:
        return ScoredCase(
            case_id=case.id,
            scorer_name=self.name,
            score=score,
            pass_=score >= self._config.pass_threshold,
            metadata=metadata,
        )


__all__ = ["NumericScorer", "NumericScorerConfig"]
