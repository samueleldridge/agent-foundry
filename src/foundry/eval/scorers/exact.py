"""ExactScorer — equality-or-near-equality (docs/40 § exact).

Score is 1.0 on match, 0.0 otherwise; fuzzy string matching (edit-distance
ratio or regex) is opt-in via ``fuzzy``. Dict expectations match as a
SUBSET: every expected key must equal the actual value at that key, extra
actual keys are ignored ('a value or partial structure').
"""

from __future__ import annotations

import difflib
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from foundry.eval.schemas import EvalCase, ScoredCase, ScorerConfig
from foundry.eval.scorers._common import (
    ScorerServices,
    parse_scorer_config,
    resolve_path,
)


class FuzzyOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["ratio", "regex"]
    """``ratio``: difflib sequence-match ratio, graded score, passes at
    ``min_ratio``. ``regex``: expected is a pattern; ``re.search`` against
    the actual string scores 1.0/0.0."""
    min_ratio: float = Field(default=0.9, ge=0.0, le=1.0)


class ExactScorerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    field: str | None = None
    """Dotted path into the actual output; None compares the whole output."""
    expected_field: str | None = None
    """Dotted path into expected. None: when ``field`` is set and expected
    is a dict carrying that path, the same path is used; otherwise expected
    is compared directly."""
    case_sensitive: bool = True
    strip: bool = False
    fuzzy: FuzzyOptions | None = None
    pass_threshold: float = Field(default=1.0, ge=0.0, le=1.0)


class ExactScorer:
    def __init__(self, config: ScorerConfig, services: ScorerServices) -> None:
        self.name = config.name
        self._config = parse_scorer_config(ExactScorerConfig, config)

    async def score(
        self, case: EvalCase, actual: Any, config: dict[str, Any]
    ) -> ScoredCase:
        cfg = self._config
        found, actual_value = resolve_path(actual, cfg.field)
        if not found:
            return self._scored(
                case, 0.0,
                metadata={"error": f"field {cfg.field!r} missing from actual"},
            )
        expected_value, err = self._expected_value(case.expected, cfg)
        if err is not None:
            return self._scored(case, 0.0, metadata={"error": err})

        score, metadata = self._compare(actual_value, expected_value, cfg)
        metadata["actual_value"] = _preview(actual_value)
        metadata["expected_value"] = _preview(expected_value)
        return self._scored(case, score, metadata=metadata)

    def _expected_value(
        self, expected: Any, cfg: ExactScorerConfig
    ) -> tuple[Any, str | None]:
        if cfg.expected_field is not None:
            found, value = resolve_path(expected, cfg.expected_field)
            if not found:
                return None, (
                    f"expected_field {cfg.expected_field!r} missing from expected"
                )
            return value, None
        if cfg.field is not None and isinstance(expected, dict):
            found, value = resolve_path(expected, cfg.field)
            if found:
                return value, None
        return expected, None

    def _compare(
        self, actual: Any, expected: Any, cfg: ExactScorerConfig
    ) -> tuple[float, dict[str, Any]]:
        if isinstance(actual, str) and isinstance(expected, str):
            return self._compare_strings(actual, expected, cfg)
        if isinstance(actual, dict) and isinstance(expected, dict):
            mismatched = sorted(
                key
                for key, value in expected.items()
                if self._compare_leaf(actual.get(key), value, cfg) < 1.0
                or key not in actual
            )
            if mismatched:
                return 0.0, {"mode": "subset", "mismatched_keys": mismatched}
            return 1.0, {"mode": "subset"}
        return (1.0 if actual == expected else 0.0), {"mode": "equality"}

    def _compare_leaf(
        self, actual: Any, expected: Any, cfg: ExactScorerConfig
    ) -> float:
        if isinstance(actual, str) and isinstance(expected, str):
            return self._compare_strings(actual, expected, cfg)[0]
        return 1.0 if actual == expected else 0.0

    def _compare_strings(
        self, actual: str, expected: str, cfg: ExactScorerConfig
    ) -> tuple[float, dict[str, Any]]:
        a, e = actual, expected
        if cfg.strip:
            a, e = a.strip(), e.strip()
        if not cfg.case_sensitive and (cfg.fuzzy is None or cfg.fuzzy.kind != "regex"):
            a, e = a.lower(), e.lower()
        if cfg.fuzzy is None:
            return (1.0 if a == e else 0.0), {"mode": "string"}
        if cfg.fuzzy.kind == "regex":
            flags = 0 if cfg.case_sensitive else re.IGNORECASE
            matched = re.search(e, a, flags) is not None
            return (1.0 if matched else 0.0), {"mode": "regex", "pattern": e}
        ratio = difflib.SequenceMatcher(None, a, e).ratio()
        score = ratio if ratio >= cfg.fuzzy.min_ratio else 0.0
        return score, {
            "mode": "ratio",
            "ratio": round(ratio, 4),
            "min_ratio": cfg.fuzzy.min_ratio,
        }

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


def _preview(value: Any, limit: int = 200) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = ["ExactScorer", "ExactScorerConfig", "FuzzyOptions"]
