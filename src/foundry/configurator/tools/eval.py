"""Eval meta-tools: ``run_eval`` / ``read_eval_results`` /
``compare_versions`` (docs/61 § Eval).

These wrap the Phase 4 harness programmatically. Eval spend is recorded
against the forge session's cost budget (docs/61: "counts against
Session.cost_budget") — a long eval CAN exhaust the forge budget, and the
meta-agent must plan around that.

The eval SET is read-only to the meta-agent (enforced at ``write_file``);
these tools only ever read it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from foundry.config import load_eval_spec
from foundry.configurator.tools.context import (
    MetaToolContext,
    RecordedEval,
    check_read_path,
)
from foundry.core.errors import ConfigError
from foundry.core.tool import RunContext
from foundry.eval import (
    AgentEvalTarget,
    EvalComparison,
    EvalRunResult,
    ProjectEvalTarget,
    cluster_failures,
    compare_project_pin_sets,
    compare_tool_versions,
    load_eval_result,
    load_tool_target,
)
from foundry.eval import run_eval as harness_run_eval

EvalScope = Literal["tool", "agent", "project"]


class RunEvalIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: EvalScope
    target: str
    """tool: 'local/<name>[@vN]' or 'catalog/<name>[@vN]'; agent: the agent
    name; project: the scoped project's name."""
    eval_spec_path: str | None = None
    """Required for scope=project; defaults to the artifact's standalone
    eval for scope=tool and the agent's eval/ set for scope=agent."""


class FailingCaseOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str
    score: float
    actual_preview: str | None = None
    error: str | None = None


class EvalRunOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval_run_id: str
    scope: str
    target: str
    score: float
    threshold: float
    passed: bool
    cases_total: int
    cases_passed: int
    cases_failed: int
    cost_usd: str | None = None
    failure_clusters: str = ""
    """cluster_failures().render() — the diagnosis surface."""
    failing_cases: list[FailingCaseOut] = Field(default_factory=list)


class ReadEvalResultsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eval_run_id: str


class CompareVersionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["tool", "project"]
    target: str
    """tool: the tool name/ref; project: the scoped project's name."""
    refs: list[str] = Field(min_length=2)
    """tool: versions ('v1', 'v2'); project: git refs ('HEAD~1', 'HEAD',
    or 'worktree' for the live tree)."""
    eval_spec_path: str | None = None
    """Required for scope=project (the project eval set)."""


class ComparisonOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labels: list[str]
    scores: list[float]
    delta: float
    """last - first; positive = the later ref improved."""
    regressions: int
    fixes: int
    verdict: Literal["improvement", "regression", "flat"]
    cost_usd: str | None = None


def _resolve_spec_path(mctx: MetaToolContext, raw: str) -> Path:
    path = Path(raw)
    if not path.is_absolute():
        candidate = mctx.project_dir / raw
        path = candidate if candidate.exists() else mctx.backend.repo_root / raw
    return path


def _summarise(
    mctx: MetaToolContext, result: EvalRunResult, spec_path: Path
) -> EvalRunOut:
    spec = load_eval_spec(spec_path)
    clustering = cluster_failures(spec, result)
    failing = [
        FailingCaseOut(
            case_id=case.case_id,
            score=case.score,
            actual_preview=case.actual_preview,
            error=(case.error or {}).get("message") if case.error else None,
        )
        for case in result.per_case
        if case.status != "skipped" and not case.pass_
    ]
    return EvalRunOut(
        eval_run_id=str(result.eval_run_id),
        scope=result.scope,
        target=result.target_ref,
        score=result.score,
        threshold=result.threshold,
        passed=result.passed,
        cases_total=result.cases_total,
        cases_passed=result.cases_passed,
        cases_failed=result.cases_failed,
        cost_usd=(
            str(result.cost_total_usd)
            if result.cost_total_usd is not None
            else None
        ),
        failure_clusters=clustering.render(),
        failing_cases=failing,
    )


def _record(
    mctx: MetaToolContext,
    ctx: RunContext,
    result: EvalRunResult,
    spec_path: Path,
) -> None:
    mctx.records.eval_runs.append(
        RecordedEval(
            scope=result.scope,
            target=result.target_ref,
            eval_run_id=str(result.eval_run_id),
            score=result.score,
            passed=result.passed,
            cost_usd=result.cost_total_usd,
            eval_spec_path=str(spec_path),
        )
    )
    budget = ctx.session.cost_budget
    if budget is not None and result.cost_total_usd is not None:
        budget.record(result.cost_total_usd)


def make_run_eval(
    mctx: MetaToolContext,
) -> Callable[[RunEvalIn, RunContext], Awaitable[EvalRunOut]]:
    async def handle(inputs: RunEvalIn, ctx: RunContext) -> EvalRunOut:
        roots = mctx.roots()
        if inputs.scope == "tool":
            ref = inputs.target
            version = None
            if "@" in ref:
                ref, version = ref.rsplit("@", 1)
            has_system = (mctx.project_dir / "system.yaml").is_file()
            target = load_tool_target(
                ref,
                roots,
                version=version or _latest_local_version(mctx, ref),
                connections_from=mctx.project_dir if has_system else None,
                secrets=mctx.secrets,
            )
            if inputs.eval_spec_path is not None:
                spec_path = _resolve_spec_path(mctx, inputs.eval_spec_path)
            else:
                rel = target.loaded.spec.standalone_eval
                if rel is None:
                    raise ConfigError(
                        f"run_eval: tool {target.ref!r} declares no "
                        "standalone eval; pass eval_spec_path",
                        context={"target": inputs.target},
                    )
                spec_path = target.loaded.directory / rel
            check_read_path(
                mctx, ctx.session, str(spec_path), tool="run_eval"
            )
            spec = load_eval_spec(spec_path)
            result = await harness_run_eval(
                spec,
                target,
                secrets=mctx.secrets,
                transport=mctx.transport,
                eval_spec_ref=str(spec_path),
            )
        else:
            from foundry.orchestration.compiler import compile_project

            # meta_authored: the scoped project is forge-written — the
            # compile boundary rejects provider_overrides even when they
            # arrive via an `extends:` base file or a case-folded filename
            # (Phase 7 review finding 1/2).
            compiled = compile_project(
                mctx.project_dir,
                secrets=mctx.secrets,
                transport=mctx.transport,
                meta_authored=True,
            )
            if inputs.scope == "project":
                if inputs.target != mctx.scoped_project:
                    raise ConfigError(
                        f"run_eval: project-scope target must be the scoped "
                        f"project {mctx.scoped_project!r}, got "
                        f"{inputs.target!r}",
                        context={"target": inputs.target},
                    )
                if inputs.eval_spec_path is None:
                    raise ConfigError(
                        "run_eval: scope=project requires eval_spec_path "
                        "(the project's eval set under evals/)",
                        context={"target": inputs.target},
                    )
                spec_path = _resolve_spec_path(mctx, inputs.eval_spec_path)
                eval_target: ProjectEvalTarget | AgentEvalTarget = (
                    ProjectEvalTarget(compiled)
                )
            else:
                if inputs.target != compiled.agent_name:
                    raise ConfigError(
                        f"run_eval: agent {inputs.target!r} is not the "
                        f"project's flow agent ({compiled.agent_name!r})",
                        context={"target": inputs.target},
                    )
                spec_path = _agent_spec_path(mctx, inputs)
                eval_target = AgentEvalTarget(compiled)
            check_read_path(
                mctx, ctx.session, str(spec_path), tool="run_eval"
            )
            spec = load_eval_spec(spec_path)
            result = await harness_run_eval(
                spec,
                eval_target,
                secrets=mctx.secrets,
                transport=mctx.transport,
                eval_spec_ref=str(spec_path),
            )
        _record(mctx, ctx, result, spec_path)
        return _summarise(mctx, result, spec_path)

    return handle


def _latest_local_version(mctx: MetaToolContext, ref: str) -> str | None:
    """Version-less local refs default to the latest on disk."""
    if not ref.startswith("local/"):
        return None
    from foundry.configurator.tools.registry import _versions_of

    versions = _versions_of(mctx.project_dir / "tools" / ref.split("/", 1)[1])
    return versions[-1] if versions else None


def _agent_spec_path(mctx: MetaToolContext, inputs: RunEvalIn) -> Path:
    if inputs.eval_spec_path is not None:
        return _resolve_spec_path(mctx, inputs.eval_spec_path)
    eval_dir = mctx.project_dir / "agents" / inputs.target / "eval"
    candidates = sorted(eval_dir.glob("*.yaml")) if eval_dir.is_dir() else []
    if len(candidates) != 1:
        raise ConfigError(
            f"run_eval: agent {inputs.target!r} needs eval_spec_path "
            f"(found {len(candidates)} candidate(s) under {eval_dir})",
            context={"target": inputs.target},
        )
    return candidates[0]


def make_read_eval_results(
    mctx: MetaToolContext,
) -> Callable[[ReadEvalResultsIn, RunContext], Awaitable[EvalRunOut]]:
    async def handle(inputs: ReadEvalResultsIn, ctx: RunContext) -> EvalRunOut:
        result = load_eval_result(inputs.eval_run_id)
        spec_ref = Path(result.eval_spec_ref) if result.eval_spec_ref else None
        if spec_ref is not None and spec_ref.is_file():
            return _summarise(mctx, result, spec_ref)
        # Spec no longer readable → summary without clustering.
        return EvalRunOut(
            eval_run_id=str(result.eval_run_id),
            scope=result.scope,
            target=result.target_ref,
            score=result.score,
            threshold=result.threshold,
            passed=result.passed,
            cases_total=result.cases_total,
            cases_passed=result.cases_passed,
            cases_failed=result.cases_failed,
            cost_usd=(
                str(result.cost_total_usd)
                if result.cost_total_usd is not None
                else None
            ),
            failure_clusters="(eval spec no longer readable; no clustering)",
            failing_cases=[
                FailingCaseOut(
                    case_id=case.case_id,
                    score=case.score,
                    actual_preview=case.actual_preview,
                    error=(
                        (case.error or {}).get("message")
                        if case.error
                        else None
                    ),
                )
                for case in result.per_case
                if case.status != "skipped" and not case.pass_
            ],
        )

    return handle


def make_compare_versions(
    mctx: MetaToolContext,
) -> Callable[[CompareVersionsIn, RunContext], Awaitable[ComparisonOut]]:
    async def handle(inputs: CompareVersionsIn, ctx: RunContext) -> ComparisonOut:
        if inputs.scope == "tool":
            comparison = await compare_tool_versions(
                inputs.target,
                list(inputs.refs),
                mctx.roots(),
                eval_path=(
                    _resolve_spec_path(mctx, inputs.eval_spec_path)
                    if inputs.eval_spec_path
                    else None
                ),
                secrets=mctx.secrets,
                transport=mctx.transport,
            )
        else:
            if inputs.target != mctx.scoped_project:
                raise ConfigError(
                    f"compare_versions: project-scope target must be "
                    f"{mctx.scoped_project!r}",
                    context={"target": inputs.target},
                )
            if inputs.eval_spec_path is None:
                raise ConfigError(
                    "compare_versions: scope=project requires "
                    "eval_spec_path",
                    context={"target": inputs.target},
                )
            comparison = await compare_project_pin_sets(
                mctx.project_dir,
                _resolve_spec_path(mctx, inputs.eval_spec_path),
                list(inputs.refs),
                secrets=mctx.secrets,
                transport=mctx.transport,
                meta_authored=True,
            )
        return _comparison_out(mctx, ctx, comparison)

    return handle


def _comparison_out(
    mctx: MetaToolContext, ctx: RunContext, comparison: EvalComparison
) -> ComparisonOut:
    total_cost = Decimal("0")
    any_cost = False
    for run in comparison.runs:
        mctx.records.eval_runs.append(
            RecordedEval(
                scope=run.scope,
                target=run.target_ref,
                eval_run_id=str(run.eval_run_id),
                score=run.score,
                passed=run.passed,
                cost_usd=run.cost_total_usd,
                eval_spec_path=run.eval_spec_ref,
            )
        )
        if run.cost_total_usd is not None:
            total_cost += run.cost_total_usd
            any_cost = True
    budget = ctx.session.cost_budget
    if budget is not None and any_cost:
        budget.record(total_cost)
    delta = comparison.summary.delta
    verdict: Literal["improvement", "regression", "flat"]
    if delta > 0:
        verdict = "improvement"
    elif delta < 0:
        verdict = "regression"
    else:
        verdict = "flat"
    return ComparisonOut(
        labels=list(comparison.labels),
        scores=[run.score for run in comparison.runs],
        delta=delta,
        regressions=comparison.summary.regressions,
        fixes=comparison.summary.fixes,
        verdict=verdict,
        cost_usd=str(total_cost) if any_cost else None,
    )


__all__ = [
    "CompareVersionsIn",
    "ComparisonOut",
    "EvalRunOut",
    "FailingCaseOut",
    "ReadEvalResultsIn",
    "RunEvalIn",
    "make_compare_versions",
    "make_read_eval_results",
    "make_run_eval",
]
