"""Eval reporting (docs/40 § Reporter): CLI tables + machine-readable JSON.

Plain text, stdlib only — the CLI prints these verbatim; ``--json`` dumps
the typed models instead.
"""

from __future__ import annotations

from foundry.eval.schemas import CaseResult, EvalComparison, EvalRunResult

_TOP_FAILURES = 8


def _fmt_duration(ms: int) -> str:
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rem = divmod(int(seconds), 60)
    return f"{minutes}m {rem:02d}s"


def render_result(result: EvalRunResult) -> str:
    """The docs/40 CLI table for one run."""
    lines: list[str] = []
    version_suffix = (
        f"@{result.target_version}"
        if result.target_version
        and not result.target_ref.endswith(f"@{result.target_version}")
        else ""
    )
    lines.append(
        f"Eval: {result.eval_name} "
        f"(scope: {result.scope}; target: {result.target_ref}{version_suffix})"
    )
    lines.append(
        f"Cases: {result.cases_total} (passed: {result.cases_passed}, "
        f"failed: {result.cases_failed}, skipped: {result.cases_skipped})"
    )
    verdict = "PASSED" if result.passed else "FAILED"
    lines.append(
        f"Score: {result.score:.2f} (threshold: {result.threshold:.2f}) "
        f"{verdict}"
    )
    cost = (
        f"; total cost: ${result.cost_total_usd}"
        if result.cost_total_usd is not None
        else ""
    )
    lines.append(f"Duration: {_fmt_duration(result.duration_ms)}{cost}")
    if "halted_reason" in result.metadata:
        lines.append(f"HALTED: {result.metadata['halted_reason']}")

    failures = sorted(
        (c for c in result.per_case if c.status != "skipped" and not c.pass_),
        key=lambda c: c.score,
    )
    if failures:
        lines.append("")
        lines.append("Top failures:")
        for case in failures[:_TOP_FAILURES]:
            reason = _failure_reason(case)
            lines.append(
                f"  {case.case_id:<44} score: {case.score:.2f}"
                + (f" ({reason})" if reason else "")
            )
        if len(failures) > _TOP_FAILURES:
            lines.append(f"  ... and {len(failures) - _TOP_FAILURES} more")

    if result.per_scorer:
        lines.append("")
        lines.append("Per-scorer:")
        for name, summary in result.per_scorer.items():
            flag = ""
            if any(
                not scored.is_deterministic
                for case in result.per_case
                for scored in case.scorer_results
                if scored.scorer_name == name
            ):
                flag = "  (non-deterministic)"
            lines.append(
                f"  {name:<28} avg {summary.average_score:.2f}  "
                f"pass% {summary.pass_rate:.2f}{flag}"
            )

    artifact = result.metadata.get("artifact_dir")
    if artifact:
        lines.append("")
        lines.append(f"Run artifact: {artifact}/eval_result.json")
    return "\n".join(lines)


def _failure_reason(case: CaseResult) -> str:
    if case.error is not None:
        return str(case.error.get("error_class", "error"))
    failed = [s for s in case.scorer_results if not s.pass_]
    if not failed:
        return ""
    return ", ".join(f"{s.scorer_name} {s.score:.2f}" for s in failed[:3])


def render_comparison(comparison: EvalComparison) -> str:
    """Side-by-side table across N runs (docs/40 § EvalComparison CLI)."""
    labels = comparison.labels
    runs = comparison.runs
    summary = comparison.summary
    width = max(10, *(len(label) + 2 for label in labels))

    lines: list[str] = []
    first = runs[0]
    lines.append(
        f"Eval: {first.eval_name} (spec {comparison.eval_spec_hash})"
    )
    lines.append(f"Target: {first.target_ref.split('@')[0]}")
    lines.append(f"Cases: {first.cases_total}")
    lines.append("")

    header = f"{'':<16}" + "".join(f"{label:>{width}}" for label in labels)
    lines.append(header)
    lines.append(
        f"{'Score':<16}"
        + "".join(f"{run.score:>{width}.2f}" for run in runs)
        + f"  (Δ {summary.delta:+.2f})"
    )
    lines.append(
        f"{'Pass rate':<16}"
        + "".join(
            f"{f'{run.cases_passed}/{run.cases_total - run.cases_skipped}':>{width}}"
            for run in runs
        )
    )
    if any(run.cost_total_usd is not None for run in runs):
        lines.append(
            f"{'Cost (USD)':<16}"
            + "".join(
                f"{(str(run.cost_total_usd) if run.cost_total_usd is not None else '-'):>{width}}"
                for run in runs
            )
        )

    if summary.per_agent:
        lines.append("")
        lines.append("Per-agent breakdown:")
        for agent, scores in summary.per_agent.items():
            delta = scores[-1] - scores[0]
            lines.append(
                f"  {agent:<24}"
                + "".join(f"{score:>{width}.2f}" for score in scores)
                + f"  ({delta:+.2f})"
            )

    regressions = [
        d for d in comparison.deltas if d.flip_direction == "regression"
    ]
    fixes = [d for d in comparison.deltas if d.flip_direction == "fix"]
    if regressions:
        lines.append("")
        lines.append(
            f"Regressions ({len(regressions)} case(s) flipped pass->fail):"
        )
        lines.extend(f"  {d.case_id}" for d in regressions)
    if fixes:
        lines.append("")
        lines.append(f"Fixes ({len(fixes)} case(s) flipped fail->pass):")
        lines.extend(f"  {d.case_id}" for d in fixes)
    if not regressions and not fixes:
        lines.append("")
        lines.append("No pass/fail flips.")
    return "\n".join(lines)


def result_json(result: EvalRunResult) -> str:
    """Full EvalRunResult for machine consumption (`--json`)."""
    return result.model_dump_json(indent=2, by_alias=True)


def comparison_json(comparison: EvalComparison) -> str:
    return comparison.model_dump_json(indent=2, by_alias=True)


__all__ = [
    "comparison_json",
    "render_comparison",
    "render_result",
    "result_json",
]
