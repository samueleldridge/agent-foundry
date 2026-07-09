"""Eval result + comparison shapes (docs/40 § EvalRunResult / EvalComparison).

The *input* shapes (``EvalSpec`` / ``EvalCase`` / ``ScorerConfig``) live in
``foundry.config.schemas`` — they are YAML-facing config like every other
spec. This module re-exports them and defines the *output* artifacts: the
typed results every eval run persists under ``~/.foundry/runs/<eval_run_id>/``
and the cross-version comparison built from them. Phase 6's meta-agent reads
these shapes directly — treat every field as API.
"""

from __future__ import annotations

import hashlib
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field

from foundry.config.schemas import EvalCase, EvalSpec, ScorerConfig
from foundry.core import RunId

EvalScorer = ScorerConfig
"""docs/03 § Phase 4 names the scorer-config type ``EvalScorer``."""


def eval_spec_hash(spec: EvalSpec) -> str:
    """Content hash of an EvalSpec — deterministic across processes (docs/40
    invariant 1). Comparisons are only valid between runs of the same hash."""
    payload = spec.model_dump_json()
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


class ScoredCase(BaseModel):
    """One scorer's judgement of one case (docs/40 § Scorer protocol)."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    case_id: str
    scorer_name: str = ""
    score: float = Field(ge=0.0, le=1.0)
    pass_: bool = Field(
        serialization_alias="pass",
        validation_alias=AliasChoices("pass", "pass_"),
    )
    is_deterministic: bool = True
    """False for scorers whose score has inherent variance (llm_judge
    without averaging); the report flags these (docs/40 § Determinism)."""
    metadata: dict[str, Any] = Field(default_factory=dict)
    """Scorer-specific debug info (fuzzy distance, judge rationale,
    rubric breakdowns, judge token/cost tallies)."""
    error: str | None = None
    """Set when the scorer itself failed; score is 0.0 in that case
    (docs/40 failure mode: scorer raises → 0.0, run continues)."""


class CaseResult(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    case_id: str
    status: Literal["scored", "error", "skipped"] = "scored"
    input_hash: str = ""
    actual: Any = None
    actual_preview: str | None = None
    score: float = 0.0
    pass_: bool = Field(
        default=False,
        serialization_alias="pass",
        validation_alias=AliasChoices("pass", "pass_"),
    )
    duration_ms: int = 0
    cost_usd: Decimal | None = None
    tokens: int = 0
    scorer_results: list[ScoredCase] = Field(default_factory=list)
    error: dict[str, Any] | None = None
    """FoundryError.to_dict() when the case errored (timeout, budget,
    target failure). Errored cases score 0.0."""
    skip_reason: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    """Harness extras (e.g. ``replicate_scores`` in non-deterministic
    mode)."""


class ScorerSummary(BaseModel):
    """Per-scorer rollup across cases: average, pass rate, percentiles."""

    model_config = ConfigDict(extra="forbid")

    scorer_name: str
    average_score: float = 0.0
    pass_rate: float = 0.0
    p50: float = 0.0
    p95: float = 0.0


class EvalRunResult(BaseModel):
    """The persisted product of one eval run (docs/40 § EvalRunResult).

    Stored as ``eval_result.json`` under ``~/.foundry/runs/<eval_run_id>/``
    with per-case detail under ``cases/``. Append-only: a new run mints a
    new ``eval_run_id``; old artifacts are immutable (docs/40 invariant 4).
    """

    model_config = ConfigDict(extra="forbid")

    eval_run_id: RunId
    eval_name: str
    scope: Literal["tool", "agent", "project", "connection", "retriever"]
    eval_spec_ref: str = ""
    """Path (or ref) the spec was loaded from; informational."""
    eval_spec_hash: str
    target_ref: str
    target_version: str = ""
    """system_version (git sha) for project scope; tool version for tool
    scope; prompt version for agent scope."""
    pin_set_hash: str = ""
    """Project/agent evals only; '' for tool scope."""
    started_at: datetime
    completed_at: datetime
    duration_ms: int = 0

    cases_total: int = 0
    cases_passed: int = 0
    cases_failed: int = 0
    cases_skipped: int = 0

    score: float = 0.0
    threshold: float = 0.9
    passed: bool = False

    per_case: list[CaseResult] = Field(default_factory=list)
    per_scorer: dict[str, ScorerSummary] = Field(default_factory=dict)

    cost_total_usd: Decimal | None = None
    tokens_total: int = 0

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Harness extras. Keys used by Phase 4: ``per_agent`` (agent name →
    score; single-agent until Phase 7), ``deterministic``, ``seed``,
    ``halted_reason`` (set when max_total_cost_usd stopped the run),
    ``artifact_dir``."""


class CaseDelta(BaseModel):
    """One case's scores across the runs under comparison (docs/40)."""

    model_config = ConfigDict(extra="forbid")

    case_id: str
    scores: list[float]
    """One per run, in the same order as ``EvalComparison.runs``."""
    delta: float
    """last - first."""
    flipped: bool
    """True when pass status differs between the first and last run."""
    flip_direction: Literal["regression", "fix"] | None = None


class ComparisonSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label_a: str
    label_b: str
    score_a: float
    score_b: float
    delta: float
    """score_b - score_a; positive = improvement."""
    regressions: int
    fixes: int
    cost_a_usd: Decimal | None = None
    cost_b_usd: Decimal | None = None
    per_agent: dict[str, list[float]] = Field(default_factory=dict)
    """Agent name → per-run scores (docs/03 exit gate: per-agent deltas).
    Single-agent until Phase 7 grows the multi-agent registry."""


class EvalComparison(BaseModel):
    """Same eval spec run against N configurations (docs/40). ``summary``
    compares the FIRST and LAST runs; ``deltas`` carries every run's
    per-case scores in order."""

    model_config = ConfigDict(extra="forbid")

    eval_spec_hash: str
    labels: list[str]
    """Human-readable name per run (tool versions, pin-set refs)."""
    runs: list[EvalRunResult]
    deltas: list[CaseDelta]
    summary: ComparisonSummary


__all__ = [
    "CaseDelta",
    "CaseResult",
    "ComparisonSummary",
    "EvalCase",
    "EvalComparison",
    "EvalRunResult",
    "EvalScorer",
    "EvalSpec",
    "ScoredCase",
    "ScorerConfig",
    "ScorerSummary",
    "eval_spec_hash",
]
