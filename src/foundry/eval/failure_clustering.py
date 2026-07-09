"""Failure clustering (docs/41 § Failure categorisation).

When an eval run has failures, ``cluster_failures`` groups them by likely
shared cause so iteration (meta-agent or human) can target the highest-
impact cluster first. The algorithm is DETERMINISTIC for the same
``EvalRunResult`` + ``EvalSpec``: stable cluster ids across re-runs make
"cluster X shrank" a real signal (docs/41 § Clustering signals).

Grouping key, per failed case:

1. **Tag overlap** — the case's (sorted) tag set from the ``EvalSpec``.
2. **Scorer-specific failures** — the set of scorer instances that failed
   the case.

Cases sharing both coordinates land in one cluster. Failed cases carrying
neither tags nor scorer verdicts (e.g. hard errors before scoring) are
``unclustered_failures``.

``impact`` is the weighted score deficit the cluster holds: fixing every
case in the cluster perfectly would raise the aggregate score by ~impact.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict, Field

from foundry.core import RunId
from foundry.eval.schemas import CaseResult, EvalRunResult, EvalSpec


class FailureCluster(BaseModel):
    """One group of failures with a likely shared cause (docs/41)."""

    model_config = ConfigDict(extra="forbid")

    cluster_id: str
    label: str
    """Human-readable: 'tag digit_sum x scorer answer_match'."""
    cases: list[CaseResult]
    suggested_diagnosis: str | None = None
    """Heuristic root-cause suggestion; ONE hypothesis among many."""
    impact: float
    """Weighted share of the aggregate score this cluster is costing.
    Higher = more leverage in fixing."""


class FailureClustering(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval_run_id: RunId
    clusters: list[FailureCluster]
    """Sorted by impact (desc), then cluster_id — deterministic."""
    unclustered_failures: list[CaseResult] = Field(default_factory=list)

    def render(self) -> str:
        """Compact text form for prompts + stdout."""
        if not self.clusters and not self.unclustered_failures:
            return "No failures."
        lines = []
        for cluster in self.clusters:
            lines.append(
                f"- cluster {cluster.cluster_id} (impact {cluster.impact:.2f}, "
                f"{len(cluster.cases)} case(s)): {cluster.label}"
            )
            if cluster.suggested_diagnosis:
                lines.append(f"  diagnosis hint: {cluster.suggested_diagnosis}")
            lines.append(
                "  cases: " + ", ".join(c.case_id for c in cluster.cases)
            )
        if self.unclustered_failures:
            lines.append(
                "- unclustered one-offs: "
                + ", ".join(c.case_id for c in self.unclustered_failures)
            )
        return "\n".join(lines)


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slug(text: str) -> str:
    return _SLUG_RE.sub("_", text.lower()).strip("_") or "misc"


def cluster_failures(
    spec: EvalSpec, result: EvalRunResult
) -> FailureClustering:
    """Group the run's failed cases into deterministic clusters."""
    cases_by_id = {case.id: case for case in spec.cases}
    total_weight = sum(
        cases_by_id[c.case_id].weight
        for c in result.per_case
        if c.status != "skipped" and c.case_id in cases_by_id
    )

    grouped: dict[tuple[str, ...], list[CaseResult]] = {}
    unclustered: list[CaseResult] = []
    for case_result in result.per_case:
        if case_result.status == "skipped" or case_result.pass_:
            continue
        spec_case = cases_by_id.get(case_result.case_id)
        tags = tuple(sorted(spec_case.tags)) if spec_case else ()
        failing_scorers = tuple(
            sorted(
                s.scorer_name
                for s in case_result.scorer_results
                if not s.pass_
            )
        )
        if not tags and not failing_scorers:
            unclustered.append(case_result)
            continue
        key = (*tags, *(f"scorer:{s}" for s in failing_scorers))
        grouped.setdefault(key, []).append(case_result)

    clusters: list[FailureCluster] = []
    for key, members in grouped.items():
        key_tags = [part for part in key if not part.startswith("scorer:")]
        scorers = [
            part[len("scorer:"):] for part in key if part.startswith("scorer:")
        ]
        deficit = sum(
            (cases_by_id[m.case_id].weight if m.case_id in cases_by_id else 1.0)
            * (1.0 - m.score)
            for m in members
        )
        impact = deficit / total_weight if total_weight > 0 else 0.0
        label_bits = []
        if key_tags:
            label_bits.append(f"tag(s) {', '.join(key_tags)}")
        if scorers:
            label_bits.append(f"failing scorer(s) {', '.join(scorers)}")
        label = " x ".join(label_bits)
        diagnosis = None
        if key_tags:
            diagnosis = (
                f"cases tagged {', '.join(key_tags)} share a failure mode — "
                "inspect their expected-vs-actual outputs for the common "
                "pattern before editing prompts or tools"
            )
        clusters.append(
            FailureCluster(
                cluster_id=_slug("_".join(key)),
                label=label,
                cases=sorted(members, key=lambda m: m.case_id),
                suggested_diagnosis=diagnosis,
                impact=round(impact, 6),
            )
        )

    clusters.sort(key=lambda c: (-c.impact, c.cluster_id))
    return FailureClustering(
        eval_run_id=result.eval_run_id,
        clusters=clusters,
        unclustered_failures=sorted(unclustered, key=lambda m: m.case_id),
    )


__all__ = ["FailureCluster", "FailureClustering", "cluster_failures"]
