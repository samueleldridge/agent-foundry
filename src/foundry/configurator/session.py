"""The forge session (docs/62): bootstrap → iterate → terminate.

One :class:`ForgeSession` is one ``foundry forge`` invocation (or one
library ``MetaAgent.forge()`` call). The session owns everything the
meta-agent must not: the budgets, the termination logic, the trajectory
artifact, and the ground truth. Scores come from RECORDED eval runs (the
meta-tool layer's :class:`ForgeRecords`), never from the meta-agent's
self-report; if an iteration ends without a project eval on record, the
session runs one itself.

Loop shape (docs/41 § The iteration loop + docs/60 § The forge loop):

- **Bootstrap** (iteration 0, only when the project has no agents): the
  meta-agent discovers the catalog, scaffolds tools/agents/configs,
  commits, and establishes the baseline score.
- **Iterate** (1..max_iter): the session clusters the last eval's
  failures, builds a directive, and the meta-agent makes ONE change +
  commit + re-eval. Regressions are the meta-agent's to roll back; the
  session records what actually happened either way.
- **Terminate** on: threshold met / max_iter / cost cap / wall-time cap /
  plateau (``no_improvement_after``) / sandbox violation / provider
  failure / operator cancel. Every termination writes the trajectory
  artifact under ``~/.foundry/runs/<forge_run_id>/``.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from foundry.config import load_eval_spec
from foundry.config.schemas import EvalSpec
from foundry.configurator.meta_agent import (
    BoundMetaAgent,
    MetaAgent,
    MetaAgentReport,
)
from foundry.configurator.tools.context import (
    VIOLATION_CANCEL_PREFIX,
    ForgeRecords,
    RecordedEval,
)
from foundry.core.errors import (
    CostBudgetExceeded,
    FoundryError,
    ProviderError,
    RunCancelled,
)
from foundry.core.events import (
    ForgeIterationCompleted,
    ForgeIterationStarted,
    ForgeRollback,
    ForgeStarted,
    ForgeTerminated,
)
from foundry.core.session import CostBudget, Session
from foundry.core.types import RunId
from foundry.eval import (
    EvalRunResult,
    ProjectEvalTarget,
    cluster_failures,
    load_eval_result,
)
from foundry.eval import run_eval as harness_run_eval
from foundry.observability.logging import run_logger
from foundry.observability.tracing import foundry_span
from foundry.runtime.execution import EventSink
from foundry.storage.paths import run_dir
from foundry.versioning.git_backend import GitBackend


class ForgeError(FoundryError):
    """Pre-flight or orchestration failure of the forge session itself."""


TerminationReason = Literal[
    "threshold_met",
    "max_iter",
    "cost_exhausted",
    "wall_time_exhausted",
    "plateau",
    "sandbox_violation",
    "provider_failure",
    "eval_infrastructure_failure",
    "user_cancelled",
]


class IterationRecord(BaseModel):
    """One line of ``trajectory.jsonl`` (docs/62 § Trajectory artifact)."""

    model_config = ConfigDict(extra="forbid")

    iteration_number: int
    kind: Literal["bootstrap", "iterate"]
    summary: str = ""
    change_kind: str | None = None
    artifact: str | None = None
    cluster_id: str | None = None
    hypothesis: str | None = None
    applied: bool = True
    rolled_back: bool = False
    commit_shas: list[str] = Field(default_factory=list)
    eval_run_id_before: str | None = None
    eval_run_id_after: str | None = None
    eval_score_before: float | None = None
    eval_score_after: float | None = None
    eval_delta: float | None = None
    duration_s: float = 0.0
    cost_usd: Decimal | None = None
    notes: str | None = None


class ForgeResult(BaseModel):
    """docs/62 § The ForgeResult."""

    model_config = ConfigDict(extra="forbid")

    forge_run_id: str
    project: str
    started_at: datetime
    completed_at: datetime
    duration_s: float

    final_score: float
    best_score: float
    threshold: float
    threshold_met: bool

    iterations: int
    """Improvement iterations run (bootstrap not counted)."""
    bootstrap: bool

    termination_reason: TerminationReason
    termination_detail: str = ""

    trajectory: list[IterationRecord] = Field(default_factory=list)
    total_cost_usd: Decimal | None = None
    total_tokens: int = 0
    meta_agent_version: str = ""
    artifact_dir: str = ""


_MAX_DIRECTIVE_CASES = 20


class ForgeSession:
    """One forge run. Construct, then ``await run()`` exactly once."""

    def __init__(
        self,
        *,
        meta_agent: MetaAgent,
        description: str,
        eval_spec_path: Path,
        threshold: float = 0.9,
        event_sink: EventSink | None = None,
    ) -> None:
        self.meta_agent = meta_agent
        self.description = description
        self.eval_spec_path = eval_spec_path
        self.threshold = threshold
        self.outer_sink = event_sink
        self.guardrails = meta_agent.guardrails

        self._sequence = 0
        self._total_tokens = 0
        self._events_file: Path | None = None

    # --- event plumbing --------------------------------------------------------

    def _sink(self, event: BaseModel) -> None:
        sequence = getattr(event, "sequence", None)
        if isinstance(sequence, int):
            self._sequence = max(self._sequence, sequence + 1)
        if getattr(event, "event", "") == "llm.completed":
            usage = getattr(event, "usage", None)
            if usage is not None:
                self._total_tokens += usage.input_tokens + usage.output_tokens
        if self._events_file is not None:
            with self._events_file.open("a") as handle:
                handle.write(event.model_dump_json() + "\n")
        if self.outer_sink is not None:
            self.outer_sink(event)

    def _emit(
        self, event_cls: type[BaseModel], run_id: str, /, **fields: Any
    ) -> None:
        event = event_cls(
            run_id=RunId.validate(run_id),
            sequence=self._sequence,
            timestamp=datetime.now(UTC),
            **fields,
        )
        self._sequence += 1
        self._sink(event)

    # --- pre-flight -------------------------------------------------------------

    def _pre_flight(self) -> tuple[Path, GitBackend, EvalSpec, Path]:
        project = self.meta_agent.scoped_project
        project_dir = self.meta_agent.projects_root / project
        if not project_dir.is_dir():
            raise ForgeError(
                f"project directory {project_dir} does not exist; create it "
                f"first: foundry project new {project}",
                context={"project": project},
            )
        backend = GitBackend.discover(project_dir)
        rel = backend.relpath(project_dir)
        if backend.is_dirty(paths=[rel]):
            raise ForgeError(
                f"working tree has uncommitted changes under {rel}; commit "
                "or stash before forging (docs/62 § Behaviour notes)",
                context={"project": project},
            )
        backend.ensure_branch(f"foundry/{project}")

        spec_path = self.eval_spec_path
        if not spec_path.is_absolute():
            candidate = project_dir / spec_path
            spec_path = (
                candidate
                if candidate.is_file()
                else backend.repo_root / self.eval_spec_path
            )
        spec = load_eval_spec(spec_path)
        if spec.scope != "project":
            raise ForgeError(
                f"forge needs a project-scope eval set; {spec_path} has "
                f"scope {spec.scope!r}",
                context={"eval_spec_path": str(spec_path)},
            )
        return project_dir, backend, spec, spec_path

    # --- directives -------------------------------------------------------------

    def _case_lines(self, spec: EvalSpec) -> str:
        lines = []
        for case in spec.cases[:_MAX_DIRECTIVE_CASES]:
            lines.append(
                f"- {case.id}: input={json.dumps(case.input)} "
                f"expected={json.dumps(case.expected)}"
                + (f" tags={case.tags}" if case.tags else "")
            )
        if len(spec.cases) > _MAX_DIRECTIVE_CASES:
            lines.append(f"- ... {len(spec.cases)} cases total")
        return "\n".join(lines)

    def _bootstrap_directive(
        self, spec: EvalSpec, spec_path: Path, budget: CostBudget | None
    ) -> str:
        project = self.meta_agent.scoped_project
        cap = (
            f"${budget.remaining_usd()}" if budget is not None else "unlimited"
        )
        return (
            "FORGE DIRECTIVE — MODE: bootstrap\n"
            f"Project: {project} (empty; scaffold it now)\n"
            f"Threshold: {self.threshold} | Improvement iterations after "
            f"bootstrap: {self.guardrails.max_iter} | Cost remaining: {cap}\n"
            "\n"
            "Description of the desired system:\n"
            f"{self.description}\n"
            "\n"
            f"Eval set (READ-ONLY; this is the target): {spec_path}\n"
            f"{self._case_lines(spec)}\n"
            "\n"
            "Do now, in order:\n"
            "1. list_catalog + list_tools — prefer catalog tools.\n"
            "2. Design the simplest system that can pass; write state.yaml "
            "and system.yaml via write_file.\n"
            "3. build_tool any genuinely project-specific tool; implement "
            "it; run_eval scope=tool until ITS eval passes BEFORE wiring "
            "it into system.yaml.\n"
            "4. build_agent the agent(s); flesh out prompts.\n"
            "5. git_commit the bootstrap (scope "
            f"'{project}', reference the artifacts).\n"
            f"6. run_eval scope=project target={project} "
            f"eval_spec_path={spec_path} to establish the baseline.\n"
            "7. Respond with action='bootstrap_complete' "
            "(notes = what you'd try first if the score is low)."
        )

    def _iterate_directive(
        self,
        *,
        iteration: int,
        spec_path: Path,
        last_result: EvalRunResult | None,
        history: list[IterationRecord],
        last_notes: str | None,
        budget: CostBudget | None,
    ) -> str:
        project = self.meta_agent.scoped_project
        spec = load_eval_spec(spec_path)
        if last_result is not None:
            clusters = cluster_failures(spec, last_result).render()
            score_line = (
                f"Current score: {last_result.score:.3f} "
                f"(threshold {self.threshold})"
            )
        else:
            clusters = "(no eval result on record)"
            score_line = "Current score: unknown"
        history_lines = "\n".join(
            f"- iter {r.iteration_number} ({r.kind}): "
            f"{r.eval_score_before if r.eval_score_before is not None else '?'}"
            f" -> {r.eval_score_after if r.eval_score_after is not None else '?'}"
            f" | {r.change_kind or '-'} | {r.summary[:80]}"
            for r in history
        )
        cap = (
            f"${budget.remaining_usd()}" if budget is not None else "unlimited"
        )
        return (
            f"FORGE DIRECTIVE — MODE: iterate (iteration {iteration} of "
            f"{self.guardrails.max_iter})\n"
            f"{score_line} | Cost remaining: {cap}\n"
            "\n"
            "Failure clusters (highest impact first):\n"
            f"{clusters}\n"
            "\n"
            "Iteration history:\n"
            f"{history_lines or '- (none)'}\n"
            "\n"
            f"Notes from your previous report: {last_notes or '(none)'}\n"
            "\n"
            "Make ONE targeted change for the highest-impact cluster "
            "(usually new_prompt_version + write_file + pin_version; or a "
            "tool fix via build_tool next-version). Then git_commit "
            f"(scope '{project}/<artifact>'), then run_eval scope=project "
            f"target={project} eval_spec_path={spec_path}. If the score "
            "regressed, rollback and say so. Respond with "
            "action='iteration_complete'."
        )

    # --- eval fallback ----------------------------------------------------------

    async def _session_project_eval(
        self,
        project_dir: Path,
        spec_path: Path,
        records: ForgeRecords,
        budget: CostBudget | None,
    ) -> EvalRunResult:
        """The session's own project eval (baseline + fallback when an
        iteration ended without one on record)."""
        from foundry.orchestration.compiler import compile_project

        spec = load_eval_spec(spec_path)
        compiled = compile_project(
            project_dir,
            secrets=self.meta_agent.secrets,
            transport=self.meta_agent.transport,
        )
        result = await harness_run_eval(
            spec,
            ProjectEvalTarget(compiled),
            secrets=self.meta_agent.secrets,
            transport=self.meta_agent.transport,
            eval_spec_ref=str(spec_path),
        )
        records.eval_runs.append(
            RecordedEval(
                scope="project",
                target=result.target_ref,
                eval_run_id=str(result.eval_run_id),
                score=result.score,
                passed=result.passed,
                cost_usd=result.cost_total_usd,
                eval_spec_path=str(spec_path),
            )
        )
        if budget is not None and result.cost_total_usd is not None:
            budget.record(result.cost_total_usd)
        return result

    # --- the loop -----------------------------------------------------------------

    async def run(self) -> ForgeResult:
        project_dir, backend, _spec, spec_path = self._pre_flight()
        project = self.meta_agent.scoped_project
        forge_run_id = str(RunId.new())
        logger = run_logger(forge_run_id)
        artifact_dir = run_dir(forge_run_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        self._events_file = artifact_dir / "events.jsonl"

        budget = (
            CostBudget(max_usd=self.guardrails.max_cost_usd)
            if self.guardrails.max_cost_usd is not None
            else None
        )
        bound = self.meta_agent.bind(forge_run_id, backend)
        records = bound.context.records
        bootstrap = self._needs_bootstrap(project_dir)
        started_at = datetime.now(UTC)
        started_clock = time.monotonic()

        self._emit(
            ForgeStarted,
            forge_run_id,
            project=project,
            forge_run_id=forge_run_id,
            meta_agent_version=self.meta_agent.version,
            max_iterations=self.guardrails.max_iter,
            max_cost_usd=self.guardrails.max_cost_usd,
            threshold=self.threshold,
        )
        logger.info(
            "forge.started",
            project=project,
            bootstrap=bootstrap,
            meta_agent_version=self.meta_agent.version,
        )

        trajectory: list[IterationRecord] = []
        last_result: EvalRunResult | None = None
        reason: TerminationReason | None = None
        detail = ""
        no_improve = 0
        last_notes: str | None = None
        best_score = 0.0

        with foundry_span(
            "foundry.forge",
            {
                "run_id": forge_run_id,
                "project": project,
                "threshold": self.threshold,
                "max_iter": self.guardrails.max_iter,
                "bootstrap": bootstrap,
            },
        ):
            if not bootstrap:
                try:
                    last_result = await self._session_project_eval(
                        project_dir, spec_path, records, budget
                    )
                    best_score = last_result.score
                except CostBudgetExceeded as exc:
                    reason, detail = "cost_exhausted", str(exc)
                except FoundryError as exc:
                    reason, detail = "eval_infrastructure_failure", str(exc)
                if reason is None and last_result is not None and (
                    last_result.score >= self.threshold
                ):
                    reason = "threshold_met"
                    detail = "baseline already meets the threshold"

            iteration_number = 0 if bootstrap else 1
            while reason is None:
                kind: Literal["bootstrap", "iterate"] = (
                    "bootstrap"
                    if bootstrap and iteration_number == 0
                    else "iterate"
                )
                iterations_done = sum(
                    1 for r in trajectory if r.kind == "iterate"
                )
                if kind == "iterate" and (
                    iterations_done >= self.guardrails.max_iter
                ):
                    reason = "max_iter"
                    detail = (
                        f"best effort: {self.guardrails.max_iter} "
                        "improvement iterations exhausted below threshold — "
                        "inspect the commits and continue manually"
                    )
                    break
                elapsed = time.monotonic() - started_clock
                if elapsed >= self.guardrails.max_wall_time_s:
                    reason = "wall_time_exhausted"
                    detail = f"wall time {elapsed:.0f}s >= cap"
                    break
                if budget is not None and budget.remaining_usd() <= 0:
                    reason = "cost_exhausted"
                    detail = (
                        f"spent ${budget.accumulated_usd} of "
                        f"${budget.max_usd}"
                    )
                    break

                record, reason, detail = await self._one_iteration(
                    bound=bound,
                    kind=kind,
                    iteration_number=iteration_number,
                    forge_run_id=forge_run_id,
                    project_dir=project_dir,
                    spec_path=spec_path,
                    budget=budget,
                    last_result=last_result,
                    last_notes=last_notes,
                    trajectory=trajectory,
                )
                trajectory.append(record)
                self._write_trajectory(artifact_dir, trajectory)
                last_notes = record.notes
                if record.eval_run_id_after is not None:
                    try:
                        last_result = load_eval_result(
                            record.eval_run_id_after
                        )
                    except FoundryError:
                        last_result = None
                if record.eval_score_after is not None:
                    best_score = max(best_score, record.eval_score_after)

                if reason is not None:
                    break
                score_after = record.eval_score_after
                if score_after is not None and score_after >= self.threshold:
                    reason = "threshold_met"
                    break
                if kind == "iterate":
                    improved = (
                        record.eval_delta is not None
                        and record.eval_delta > 1e-9
                    )
                    no_improve = 0 if improved else no_improve + 1
                    if no_improve >= self.guardrails.no_improvement_after:
                        reason = "plateau"
                        detail = (
                            f"no improvement across {no_improve} "
                            "consecutive iterations"
                        )
                        break
                iteration_number += 1

        completed_at = datetime.now(UTC)
        final_score = (
            trajectory[-1].eval_score_after
            if trajectory and trajectory[-1].eval_score_after is not None
            else (last_result.score if last_result is not None else 0.0)
        )
        result = ForgeResult(
            forge_run_id=forge_run_id,
            project=project,
            started_at=started_at,
            completed_at=completed_at,
            duration_s=time.monotonic() - started_clock,
            final_score=final_score,
            best_score=max(best_score, final_score),
            threshold=self.threshold,
            threshold_met=reason == "threshold_met",
            iterations=sum(1 for r in trajectory if r.kind == "iterate"),
            bootstrap=bootstrap,
            termination_reason=reason or "provider_failure",
            termination_detail=detail,
            trajectory=trajectory,
            total_cost_usd=(
                budget.accumulated_usd if budget is not None else None
            ),
            total_tokens=self._total_tokens,
            meta_agent_version=self.meta_agent.version,
            artifact_dir=str(artifact_dir),
        )
        self._emit(
            ForgeTerminated,
            forge_run_id,
            forge_run_id=forge_run_id,
            reason=result.termination_reason,
            final_score=result.final_score,
            iterations=result.iterations,
            total_cost_usd=result.total_cost_usd,
        )
        logger.info(
            "forge.terminated",
            project=project,
            reason=result.termination_reason,
            final_score=result.final_score,
            iterations=result.iterations,
        )
        self._write_artifacts(artifact_dir, result)
        return result

    # --- one iteration ---------------------------------------------------------

    async def _one_iteration(
        self,
        *,
        bound: BoundMetaAgent,
        kind: Literal["bootstrap", "iterate"],
        iteration_number: int,
        forge_run_id: str,
        project_dir: Path,
        spec_path: Path,
        budget: CostBudget | None,
        last_result: EvalRunResult | None,
        last_notes: str | None,
        trajectory: list[IterationRecord],
    ) -> tuple[IterationRecord, TerminationReason | None, str]:
        records = bound.context.records
        spec = load_eval_spec(spec_path)
        directive = (
            self._bootstrap_directive(spec, spec_path, budget)
            if kind == "bootstrap"
            else self._iterate_directive(
                iteration=iteration_number,
                spec_path=spec_path,
                last_result=last_result,
                history=trajectory,
                last_notes=last_notes,
                budget=budget,
            )
        )
        self._emit(
            ForgeIterationStarted,
            forge_run_id,
            forge_run_id=forge_run_id,
            iteration_number=iteration_number,
            directive_kind=kind,
        )
        mark = records.mark()
        cost_before = (
            budget.accumulated_usd if budget is not None else Decimal("0")
        )
        started = time.monotonic()
        step_session = Session.new(
            project=self.meta_agent.scoped_project,
            cost_budget=budget,
            logger=run_logger(forge_run_id),
        )
        report: MetaAgentReport | None = None
        reason: TerminationReason | None = None
        detail = ""
        try:
            report = await bound.step(
                directive,
                step_session,
                self._sink,
                start_sequence=self._sequence,
            )
        except CostBudgetExceeded as exc:
            reason, detail = "cost_exhausted", str(exc)
        except RunCancelled as exc:
            cancel_reason = step_session.cancel_token.reason or str(exc)
            if cancel_reason.startswith(VIOLATION_CANCEL_PREFIX):
                reason, detail = "sandbox_violation", cancel_reason
            else:
                reason, detail = "user_cancelled", cancel_reason
        except ProviderError as exc:
            reason, detail = "provider_failure", str(exc)
        except FoundryError as exc:
            reason, detail = "provider_failure", (
                f"{type(exc).__name__}: {exc}"
            )

        activity = records.since(mark)
        if reason is None and activity.violations:
            reason = "sandbox_violation"
            detail = "; ".join(
                f"{v.tool}: {v.detail}" for v in activity.violations
            )

        project_evals = [
            e for e in activity.eval_runs if e.scope == "project"
        ]
        score_after: float | None = None
        eval_run_id_after: str | None = None
        if project_evals:
            score_after = project_evals[-1].score
            eval_run_id_after = project_evals[-1].eval_run_id
        elif reason is None:
            # The meta-agent didn't run the project eval — the session does,
            # so the trajectory always carries authoritative scores.
            try:
                fallback = await self._session_project_eval(
                    project_dir, spec_path, records, budget
                )
                score_after = fallback.score
                eval_run_id_after = str(fallback.eval_run_id)
            except CostBudgetExceeded as exc:
                reason, detail = "cost_exhausted", str(exc)
            except FoundryError as exc:
                reason, detail = "eval_infrastructure_failure", (
                    f"post-iteration eval could not run: {exc}"
                )

        score_before = last_result.score if last_result is not None else None
        for rollback in activity.rollbacks:
            self._emit(
                ForgeRollback,
                forge_run_id,
                forge_run_id=forge_run_id,
                iteration_number=iteration_number,
                scope=rollback.scope,
                target=rollback.target,
                to_version=rollback.to_version,
            )
        record = IterationRecord(
            iteration_number=iteration_number,
            kind=kind,
            summary=(
                report.summary
                if report is not None
                else (detail or "(iteration aborted)")
            ),
            change_kind=(
                report.change_kind
                if report is not None
                else ("bootstrap" if kind == "bootstrap" else None)
            ),
            artifact=report.artifact if report is not None else None,
            cluster_id=report.cluster_id if report is not None else None,
            hypothesis=report.hypothesis if report is not None else None,
            applied=(
                report.applied if report is not None else bool(activity.commits)
            ),
            rolled_back=(
                (report.rolled_back if report is not None else False)
                or bool(activity.rollbacks)
            ),
            commit_shas=[c.sha for c in activity.commits],
            eval_run_id_before=(
                str(last_result.eval_run_id) if last_result is not None else None
            ),
            eval_run_id_after=eval_run_id_after,
            eval_score_before=score_before,
            eval_score_after=score_after,
            eval_delta=(
                score_after - score_before
                if score_after is not None and score_before is not None
                else None
            ),
            duration_s=time.monotonic() - started,
            cost_usd=(
                budget.accumulated_usd - cost_before
                if budget is not None
                else None
            ),
            notes=report.notes if report is not None else None,
        )
        self._emit(
            ForgeIterationCompleted,
            forge_run_id,
            forge_run_id=forge_run_id,
            iteration_number=iteration_number,
            eval_score=score_after,
            eval_delta=record.eval_delta,
            commit_shas=record.commit_shas,
            cluster_id=record.cluster_id,
            applied=record.applied,
        )
        return record, reason, detail

    # --- artifacts ----------------------------------------------------------------

    def _needs_bootstrap(self, project_dir: Path) -> bool:
        system = project_dir / "system.yaml"
        if not system.is_file():
            return True
        import yaml

        try:
            data = yaml.safe_load(system.read_text())
        except yaml.YAMLError:
            return True
        agents = data.get("agents") if isinstance(data, dict) else None
        return not agents

    def _write_trajectory(
        self, artifact_dir: Path, trajectory: list[IterationRecord]
    ) -> None:
        path = artifact_dir / "trajectory.jsonl"
        with path.open("w") as handle:
            for record in trajectory:
                handle.write(record.model_dump_json() + "\n")

    def _write_artifacts(self, artifact_dir: Path, result: ForgeResult) -> None:
        self._write_trajectory(artifact_dir, result.trajectory)
        (artifact_dir / "meta.json").write_text(
            result.model_dump_json(indent=2, exclude={"trajectory"}) + "\n"
        )
        (artifact_dir / "final_summary.md").write_text(render_summary(result))


def render_summary(result: ForgeResult) -> str:
    """The human-readable summary (stdout + final_summary.md)."""
    lines = [
        f"# Forge {result.forge_run_id} — {result.project}",
        "",
        f"- Termination: {result.termination_reason}"
        + (f" ({result.termination_detail})" if result.termination_detail else ""),
        f"- Final score: {result.final_score:.3f} "
        f"(best {result.best_score:.3f}; threshold {result.threshold})",
        f"- Iterations: {result.iterations}"
        + (" + bootstrap" if result.bootstrap else ""),
        "- Cost: "
        + (
            f"${result.total_cost_usd}"
            if result.total_cost_usd is not None
            else "(no budget tracking)"
        )
        + f" | Tokens: {result.total_tokens}",
        f"- Duration: {result.duration_s:.1f}s",
        f"- Meta-agent: {result.meta_agent_version}",
        "",
        "## Trajectory",
        "",
    ]
    for record in result.trajectory:
        before = (
            f"{record.eval_score_before:.3f}"
            if record.eval_score_before is not None
            else "-"
        )
        after = (
            f"{record.eval_score_after:.3f}"
            if record.eval_score_after is not None
            else "-"
        )
        commits = ", ".join(sha[:8] for sha in record.commit_shas) or "-"
        lines.append(
            f"- iter {record.iteration_number} ({record.kind}): "
            f"{before} -> {after} | {record.change_kind or '-'} | "
            f"commits: {commits}"
            + (" | ROLLED BACK" if record.rolled_back else "")
            + f" | {record.summary[:100]}"
        )
    if result.termination_reason in ("max_iter", "plateau"):
        lines += [
            "",
            "Best-effort state: the threshold was not met. Inspect the "
            "commits above (`foundry versions <project>`), then continue "
            "manually or re-forge with a higher budget.",
        ]
    return "\n".join(lines) + "\n"


__all__ = [
    "ForgeError",
    "ForgeResult",
    "ForgeSession",
    "IterationRecord",
    "TerminationReason",
    "render_summary",
]
