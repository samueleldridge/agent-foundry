"""The async eval runner (docs/40 § Runner): three scopes, ONE harness.

Targets differ — a tool version dispatches straight through a ToolRegistry
(no agent in the loop), an agent runs its step loop in isolation, a project
runs the full compiled system — but case orchestration, scoring, aggregation
and artifact writing are shared. Per case the harness mints an eval-scoped
``RunId``, builds a ``Session`` (with the case cost budget), invokes the
target under the case timeout, scores the output with every configured
scorer, and rolls everything up into an ``EvalRunResult`` persisted under
``~/.foundry/runs/<eval_run_id>/`` (Phase 6 reads these artifacts).

Import note: LangGraph is only touched lazily (inside the project-scope
invoke) via the runtime adapter — this module itself stays langgraph-free.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from foundry.catalog.loader import LoadedToolVersion, load_tool_version
from foundry.config import (
    ArtifactRef,
    EnvSecretsProvider,
    FoundryRoots,
    load_system_spec,
)
from foundry.config.secrets import SecretsProvider
from foundry.connections import (
    InProcessConnectionPool,
    PreparedConnection,
    SlotConnectionAccessor,
    prepare_connection,
    validate_tool_connection_wiring,
)
from foundry.core import (
    ConnectionContext,
    CostBudget,
    LLMCallCompleted,
    RegisteredTool,
    RunId,
    Session,
    ToolDescriptor,
    ToolRegistry,
    WarningEvent,
)
from foundry.core.errors import (
    CompileError,
    ConfigLoadError,
    ConfigValidationError,
    FoundryError,
    OrchestrationError,
)
from foundry.core.tool import RunContext, input_hash
from foundry.eval.schemas import (
    CaseResult,
    EvalCase,
    EvalRunResult,
    EvalSpec,
    ScoredCase,
    ScorerConfig,
    ScorerSummary,
    eval_spec_hash,
)
from foundry.eval.scorers import Scorer, ScorerServices, build_scorers
from foundry.eval.scorers.llm_judge import judge_cost
from foundry.observability.tracing import foundry_span, set_span_attributes
from foundry.retrieval import build_retriever_accessor
from foundry.runtime.compiled import CompiledProject
from foundry.runtime.execution import (
    AgentStepRuntime,
    EventEmitter,
    EventSink,
    RunCounters,
    seed_state,
)
from foundry.storage.paths import run_dir

# --- targets --------------------------------------------------------------------


@dataclass(frozen=True)
class ToolEvalTarget:
    """One tool version, invoked through the dispatcher — NOT via an agent."""

    loaded: LoadedToolVersion
    slots: dict[str, PreparedConnection] | None = None
    """Connection bindings for connection-requiring tools (docs/40: 'a test
    fixture or the project's bound connection'). None for pure tools."""
    project: str = ""
    """Pool scope when slots are bound (the lending project's name)."""

    scope = "tool"

    @property
    def ref(self) -> str:
        return self.loaded.ref.to_str()

    @property
    def version(self) -> str:
        return self.loaded.ref.version


@dataclass(frozen=True)
class AgentEvalTarget:
    """The compiled project's agent, run in isolation (its step loop only —
    no surrounding function nodes). Single-agent until Phase 7."""

    compiled: CompiledProject

    scope = "agent"

    @property
    def ref(self) -> str:
        return self.compiled.agent_name

    @property
    def version(self) -> str:
        return self.compiled.agent.spec.prompt.version


@dataclass(frozen=True)
class ProjectEvalTarget:
    """The whole compiled system, end to end."""

    compiled: CompiledProject

    scope = "project"

    @property
    def ref(self) -> str:
        return self.compiled.project.system.name

    @property
    def version(self) -> str:
        return self.compiled.system_version


EvalTarget = ToolEvalTarget | AgentEvalTarget | ProjectEvalTarget


def load_tool_target(
    ref_str: str,
    roots: FoundryRoots,
    *,
    version: str | None = None,
    connections_from: Path | None = None,
    secrets: SecretsProvider | None = None,
) -> ToolEvalTarget:
    """Resolve ``catalog/<name>@v<N>`` (or local/) into a tool eval target.

    Connection-requiring tools (Phase 5 closes the Phase 4 seam): pass
    ``connections_from`` — a project directory whose ``system.yaml`` binds
    the tool — and the standalone eval runs against the project's bound
    connections (docs/40 § Three scopes: 'a test fixture or the project's
    bound connection'). Without it, a tool whose spec requires a
    non-optional connection slot is refused with a structured error.
    """
    ref = ArtifactRef.parse(ref_str, "tool", version=version)
    loaded = load_tool_version(ref, roots)
    required = [
        slot.slot for slot in loaded.spec.connections_required if not slot.optional
    ]
    if not required:
        return ToolEvalTarget(loaded=loaded)
    if connections_from is None:
        raise CompileError(
            f"tool {ref.to_str()!r} requires connection slot(s) "
            f"{', '.join(required)}; standalone tool evals need a project "
            "that binds them — pass --project <dir> (its system.yaml "
            "connection_bindings for this tool are used)",
            context={"ref": ref.to_str(), "required_slots": required},
        )
    slots, project_name = _project_bound_slots(
        loaded, ref, connections_from, secrets or EnvSecretsProvider()
    )
    return ToolEvalTarget(loaded=loaded, slots=slots, project=project_name)


def _project_bound_slots(
    loaded: LoadedToolVersion,
    ref: ArtifactRef,
    project_dir: Path,
    secrets: SecretsProvider,
) -> tuple[dict[str, PreparedConnection], str]:
    """Borrow a project's connection bindings for a standalone tool eval."""
    system_file = project_dir / "system.yaml"
    system = load_system_spec(system_file)
    bare_ref = f"{ref.scope}/{ref.name}"
    candidates = [
        (name, binding)
        for name, binding in system.tools.items()
        if binding.ref == bare_ref
    ]
    if not candidates:
        raise CompileError(
            f"project {system.name!r} does not bind tool {bare_ref!r} in "
            f"{system_file}; bind it (with connection_bindings) so its "
            "standalone eval can run against real connections",
            context={
                "ref": ref.to_str(),
                "project": system.name,
                "bound_tools": sorted(system.tools),
            },
        )
    logical_name, binding = candidates[0]
    project_roots = FoundryRoots.for_project(project_dir)
    prepared = {
        conn_name: prepare_connection(
            conn_name,
            system.connections[conn_name],
            project_roots,
            secrets,
            system_file=system_file,
        )
        for conn_name in sorted(set(binding.connection_bindings.values()))
        if conn_name in system.connections
    }
    slots = validate_tool_connection_wiring(
        logical_name, loaded.spec, binding, prepared, system_file=system_file
    )
    return slots, system.name


# --- case invocation ---------------------------------------------------------------

Invoke = Callable[
    [EvalCase, Session, EventEmitter, EventSink], Awaitable[Any]
]


def _tool_invoke(
    loaded: LoadedToolVersion,
    *,
    slots: dict[str, PreparedConnection] | None = None,
    project: str = "",
    transport: httpx.AsyncBaseTransport | None = None,
) -> Invoke:
    registry = ToolRegistry()
    name = loaded.spec.name
    registry.register(
        RegisteredTool(
            descriptor=ToolDescriptor(
                name=name,
                ref=f"{loaded.ref.scope}/{loaded.ref.name}",
                version=loaded.ref.version,
                description=loaded.spec.description,
                tags=loaded.spec.tags,
                connection_slots=sorted(slots) if slots else [],
            ),
            input_schema=loaded.input_model,
            output_schema=loaded.output_model,
            handler=loaded.handler,
            timeout_s=float(loaded.spec.timeout_s),
            retry_policy=loaded.spec.retry_policy,
            auth_error_retry=any(
                p.refresh.mode == "on_auth_error" for p in (slots or {}).values()
            ),
            # Caches are never required for eval correctness (docs/40
            # § Composition); standalone tool evals run cache-less.
            cacheable=False,
        )
    )

    async def invoke(
        case: EvalCase, session: Session, emitter: EventEmitter, sink: EventSink
    ) -> Any:
        pool: InProcessConnectionPool | None = None
        accessor: SlotConnectionAccessor | None = None
        if slots:
            pool = InProcessConnectionPool()
            accessor = SlotConnectionAccessor(
                pool,
                project or "standalone_eval",
                slots,
                ConnectionContext(http_transport=transport),
                agent_name="__eval__",
                emit=emitter.emit,
            )
        try:
            return await _dispatch(case, session, emitter, accessor)
        finally:
            if accessor is not None:
                await accessor.release_all()
            if pool is not None:
                await pool.close_all()

    async def _dispatch(
        case: EvalCase,
        session: Session,
        emitter: EventEmitter,
        connections: SlotConnectionAccessor | None,
    ) -> Any:
        ctx = RunContext(
            run_id=str(session.run_id),
            agent_name="__eval__",
            session=session,
            tool_ref=loaded.ref.to_str(),
            timeout_s=float(loaded.spec.timeout_s),
            retry_policy=loaded.spec.retry_policy,
            connections=connections,
            retrievers=None,
        )
        return await registry.dispatch(
            name, [name], dict(case.input), ctx, emit=emitter.emit
        )

    return invoke


def _agent_invoke(compiled: CompiledProject) -> Invoke:
    """Drive the agent's step loop directly (the same node-sized slices the
    graph runs, docs/_phase_handoffs/phase_3.md) — agent in ISOLATION: no
    flow-level function nodes, no checkpointer."""

    async def invoke(
        case: EvalCase, session: Session, emitter: EventEmitter, sink: EventSink
    ) -> Any:
        pool = InProcessConnectionPool()
        runtime = AgentStepRuntime(
            compiled, session, emitter, pool, RunCounters()
        )
        conn_accessors: list[Any] = []
        try:
            if compiled.retrievers:
                accessor, conn_accessors = await build_retriever_accessor(
                    compiled.retrievers,
                    pool=pool,
                    project=compiled.project.system.name,
                    project_dir=compiled.project.directory,
                    agent_name=compiled.agent_name,
                    secrets=compiled.secrets,
                    transport=compiled.transport,
                    emit=emitter.emit,
                )
                runtime.retrievers = accessor

            state = seed_state(compiled, dict(case.input))
            conv: dict[str, Any] = {}
            output: Any = None

            def apply(update: dict[str, Any]) -> None:
                nonlocal conv, state, output
                if "conv" in update:
                    conv = update["conv"] or {}
                if "state" in update:
                    state = update["state"]
                if "output" in update:
                    output = update["output"]

            apply(await runtime.begin(conv, state))
            label = runtime.route_after_begin(conv)
            while label != "finish":
                if label == "llm":
                    apply(await runtime.llm_round(conv, state))
                    label = runtime.route_after_llm(conv)
                elif label == "tools":
                    apply(await runtime.dispatch_tools(conv, state))
                    label = "llm"
                elif label == "turn":
                    apply(await runtime.start_turn(conv, state))
                    label = "llm"
                elif label == "turn_end":
                    apply(await runtime.end_turn(conv, state))
                    label = runtime.route_after_turn_end(conv)
                else:  # pragma: no cover - routing vocabulary is closed
                    raise OrchestrationError(
                        f"agent step routed to unknown label {label!r}",
                        context={"label": label},
                    )
            apply(await runtime.finish(conv, state))
            return output
        finally:
            for accessor in conn_accessors:
                await accessor.release_all()
            await pool.close_all()

    return invoke


def _project_invoke(compiled: CompiledProject) -> Invoke:
    async def invoke(
        case: EvalCase, session: Session, emitter: EventEmitter, sink: EventSink
    ) -> Any:
        # The ONLY langgraph-adjacent import in the eval layer; lazy so
        # tool-scope evals never touch the graph runtime.
        from foundry.runtime.langgraph_adapter import run_project

        result = await run_project(
            compiled, dict(case.input), session, sink, checkpointer="none"
        )
        return result.output

    return invoke


def _build_invoke(
    target: EvalTarget, transport: httpx.AsyncBaseTransport | None = None
) -> Invoke:
    if isinstance(target, ToolEvalTarget):
        return _tool_invoke(
            target.loaded,
            slots=target.slots,
            project=target.project,
            transport=transport,
        )
    if isinstance(target, AgentEvalTarget):
        return _agent_invoke(target.compiled)
    return _project_invoke(target.compiled)


# --- load-time validation -------------------------------------------------------------


def validate_cases(spec: EvalSpec, target: EvalTarget) -> None:
    """Every case's input validates against the target's input surface at
    LOAD time — bad inputs never waste run time (docs/40 invariant 2)."""
    for case in spec.cases:
        if case.skip:
            continue
        if isinstance(target, ToolEvalTarget):
            try:
                target.loaded.input_model.model_validate(case.input)
            except ValidationError as exc:
                first = exc.errors()[0]
                raise ConfigValidationError(
                    f"eval case {case.id!r} input fails the tool's input "
                    f"schema {target.loaded.input_model.__name__}: "
                    f"{first['msg']} (at "
                    f"{'/'.join(str(p) for p in first['loc'])})",
                    context={"case_id": case.id, "eval": spec.name},
                    cause=exc,
                ) from exc
        elif isinstance(target, AgentEvalTarget):
            view = target.compiled.compiled_state.agent_views[
                target.compiled.agent_name
            ]
            unknown = sorted(set(case.input) - set(view.read))
            if unknown:
                raise ConfigValidationError(
                    f"eval case {case.id!r} input has field(s) outside agent "
                    f"{target.compiled.agent_name!r}'s read scope: "
                    f"{', '.join(unknown)} (readable: "
                    f"{', '.join(sorted(view.read))})",
                    context={"case_id": case.id, "eval": spec.name,
                             "unknown_fields": unknown},
                )
        else:
            schema = target.compiled.project.state.state_schema
            unknown = sorted(set(case.input) - set(schema))
            if unknown:
                raise ConfigValidationError(
                    f"eval case {case.id!r} input has field(s) not in the "
                    f"project state schema: {', '.join(unknown)} (declared: "
                    f"{', '.join(sorted(schema))})",
                    context={"case_id": case.id, "eval": spec.name,
                             "unknown_fields": unknown},
                )


def _check_scope(spec: EvalSpec, target: EvalTarget) -> None:
    if spec.scope != target.scope:
        raise ConfigValidationError(
            f"eval {spec.name!r} declares scope {spec.scope!r} but the "
            f"target is {target.scope}-scoped ({target.ref})",
            context={"eval": spec.name, "spec_scope": spec.scope,
                     "target_scope": target.scope},
        )


def _apply_determinism(
    spec: EvalSpec, target: EvalTarget, emitter: EventEmitter
) -> None:
    """docs/40 § Determinism: fix Python's RNG, force temperature 0 on the
    target's model binding, propagate seed where the provider supports it
    (best-effort + warning where not)."""
    if not spec.deterministic:
        return
    if spec.seed is not None:
        random.seed(spec.seed)
    if isinstance(target, ToolEvalTarget):
        return
    compiled = target.compiled
    settings = compiled.agent.spec.model_binding.settings
    settings.temperature = 0.0
    if spec.seed is None:
        return
    if compiled.provider.capabilities.seed:
        settings.seed = spec.seed
    else:
        emitter.emit(
            WarningEvent,
            agent_name=compiled.agent_name,
            category="eval.determinism.seed_unsupported",
            message=(
                f"deterministic eval requested seed {spec.seed} but "
                f"{compiled.provider.name}/{compiled.provider.model} does "
                "not support seed; continuing best-effort (docs/40)"
            ),
            error_class=None,
        )


# --- the runner -------------------------------------------------------------------


async def run_eval(
    spec: EvalSpec,
    target: EvalTarget,
    *,
    secrets: SecretsProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    event_sink: EventSink | None = None,
    eval_spec_ref: str = "",
    write_artifact: bool = True,
    extra_metadata: dict[str, Any] | None = None,
) -> EvalRunResult:
    """Run one eval set against one target; returns (and by default
    persists) the ``EvalRunResult``.

    In deterministic mode this MUTATES the compiled target's model-binding
    settings (temperature 0 + seed) — compile a fresh target per eval run.
    """
    _check_scope(spec, target)
    validate_cases(spec, target)

    if isinstance(target, ToolEvalTarget):
        secrets = secrets or EnvSecretsProvider()
    else:
        secrets = secrets or target.compiled.secrets
        transport = transport or target.compiled.transport

    eval_run_id = RunId.new()
    started_at = datetime.now(UTC)
    started_clock = time.monotonic()
    spec_hash = eval_spec_hash(spec)

    # Eval-scoped session: judge calls run on it, so judge spend counts
    # against the eval's total budget and judge events carry the eval run id.
    eval_session = Session.new(
        project=target.ref,
        run_id=eval_run_id,
        cost_budget=(
            CostBudget(max_usd=spec.max_total_cost_usd)
            if spec.max_total_cost_usd is not None
            else None
        ),
    )
    eval_emitter = EventEmitter(eval_session, event_sink)

    scorers = build_scorers(
        spec,
        ScorerServices(
            secrets=secrets,
            transport=transport,
            emit=eval_emitter.emit,
            judge_session=eval_session,
            deterministic=spec.deterministic,
            seed=spec.seed,
        ),
    )
    _apply_determinism(spec, target, eval_emitter)
    invoke = _build_invoke(target, transport)

    semaphore = asyncio.Semaphore(spec.max_parallel)
    total_cost = Decimal("0")
    halted_reason: str | None = None

    async def guarded(case: EvalCase) -> CaseResult:
        nonlocal total_cost, halted_reason
        async with semaphore:
            if case.skip:
                return CaseResult(
                    case_id=case.id,
                    status="skipped",
                    skip_reason=case.skip_reason or "skip: true",
                )
            if halted_reason is not None:
                return CaseResult(
                    case_id=case.id, status="skipped", skip_reason=halted_reason
                )
            result = await _run_case(
                spec, case, target, invoke, scorers, event_sink
            )
            if result.cost_usd is not None:
                total_cost += result.cost_usd
                if (
                    spec.max_total_cost_usd is not None
                    and total_cost > spec.max_total_cost_usd
                ):
                    halted_reason = (
                        f"max_total_cost_usd {spec.max_total_cost_usd} "
                        f"exceeded (spent {total_cost}); remaining cases "
                        "skipped"
                    )
            return result

    with foundry_span(
        "foundry.eval",
        {
            "run_id": str(eval_run_id),
            "eval_name": spec.name,
            "eval_spec_hash": spec_hash,
            "scope": spec.scope,
            "target": target.ref,
            "target_version": target.version,
            "cases": len(spec.cases),
        },
    ) as span:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(guarded(case)) for case in spec.cases]
        per_case = [task.result() for task in tasks]
        result = _aggregate(
            spec,
            target,
            per_case,
            eval_run_id=eval_run_id,
            spec_hash=spec_hash,
            eval_spec_ref=eval_spec_ref,
            started_at=started_at,
            duration_ms=int((time.monotonic() - started_clock) * 1000),
            halted_reason=halted_reason,
        )
        if extra_metadata:
            result.metadata.update(extra_metadata)
        set_span_attributes(
            span,
            {
                "score": result.score,
                "passed": result.passed,
                "cases_passed": result.cases_passed,
                "cases_failed": result.cases_failed,
                "cases_skipped": result.cases_skipped,
                "cost_total_usd": (
                    str(result.cost_total_usd)
                    if result.cost_total_usd is not None
                    else None
                ),
            },
        )

    if write_artifact:
        project_dir = (
            target.compiled.project.directory
            if not isinstance(target, ToolEvalTarget)
            else None
        )
        directory = write_eval_artifact(result, project_dir=project_dir)
        result.metadata["artifact_dir"] = str(directory)
        # re-write with the artifact_dir recorded (self-describing artifact)
        (directory / "eval_result.json").write_text(
            result.model_dump_json(indent=2, by_alias=True) + "\n"
        )
    return result


async def _run_case(
    spec: EvalSpec,
    case: EvalCase,
    target: EvalTarget,
    invoke: Invoke,
    scorers: list[tuple[ScorerConfig, Scorer]],
    outer_sink: EventSink | None,
) -> CaseResult:
    events: list[BaseModel] = []

    def sink(event: BaseModel) -> None:
        events.append(event)
        if outer_sink is not None:
            outer_sink(event)

    session = Session.new(
        project=target.ref,
        cost_budget=(
            CostBudget(max_usd=spec.case_max_cost_usd)
            if spec.case_max_cost_usd is not None
            else None
        ),
    )
    emitter = EventEmitter(session, sink)
    hashed = input_hash(dict(case.input))
    started = time.monotonic()

    replicates = spec.replicates if not spec.deterministic else 1
    replicate_scores: list[float] = []
    scorer_results: list[ScoredCase] = []
    actual: Any = None

    for _ in range(replicates):
        try:
            async with asyncio.timeout(spec.case_timeout_s):
                raw = await invoke(case, session, emitter, sink)
        except FoundryError as exc:
            return _error_case(case, hashed, exc.to_dict(), started)
        except TimeoutError:
            timeout_error = OrchestrationError(
                f"eval case {case.id!r} exceeded case_timeout_s of "
                f"{spec.case_timeout_s}s",
                context={"case_id": case.id,
                         "case_timeout_s": spec.case_timeout_s},
            )
            return _error_case(case, hashed, timeout_error.to_dict(), started)

        actual = _jsonable(raw)
        scorer_results = []
        for config, scorer in scorers:
            try:
                scored = await scorer.score(case, actual, dict(config.config))
            except Exception as exc:  # scorer failure ≠ run failure (docs/40)
                emitter.emit(
                    WarningEvent,
                    agent_name=config.name,
                    category="eval.scorer.error",
                    message=f"scorer {config.name!r} failed on case "
                    f"{case.id!r}; recorded 0.0: {exc}",
                    error_class=type(exc).__name__,
                )
                scored = ScoredCase(
                    case_id=case.id,
                    scorer_name=config.name,
                    score=0.0,
                    pass_=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            scorer_results.append(scored)
        replicate_scores.append(_weighted_case_score(scorers, scorer_results))

    score = sum(replicate_scores) / len(replicate_scores)
    tokens, cost = _case_usage(events, scorer_results)
    metadata: dict[str, Any] = {}
    if len(replicate_scores) > 1:
        metadata["replicate_scores"] = replicate_scores
    return CaseResult(
        case_id=case.id,
        status="scored",
        input_hash=hashed,
        actual=actual,
        actual_preview=_preview(actual),
        score=score,
        pass_=score >= spec.threshold,
        duration_ms=int((time.monotonic() - started) * 1000),
        cost_usd=cost,
        tokens=tokens,
        scorer_results=scorer_results,
        metadata=metadata,
    )


def _error_case(
    case: EvalCase, hashed: str, error: dict[str, Any], started: float
) -> CaseResult:
    return CaseResult(
        case_id=case.id,
        status="error",
        input_hash=hashed,
        score=0.0,
        pass_=False,
        duration_ms=int((time.monotonic() - started) * 1000),
        error=error,
    )


def _weighted_case_score(
    scorers: list[tuple[ScorerConfig, Scorer]],
    results: list[ScoredCase],
) -> float:
    total = sum(config.weight for config, _ in scorers)
    if total <= 0:
        return 0.0
    weighted = sum(
        config.weight * scored.score
        for (config, _), scored in zip(scorers, results, strict=True)
    )
    return weighted / total


def _case_usage(
    events: list[BaseModel], scorer_results: list[ScoredCase]
) -> tuple[int, Decimal | None]:
    """Tokens/cost from the case's LLM events plus judge tallies (judge
    calls emit on the EVAL session, so they arrive via scorer metadata)."""
    tokens = 0
    cost: Decimal | None = None
    for event in events:
        if isinstance(event, LLMCallCompleted):
            tokens += event.usage.input_tokens + event.usage.output_tokens
            if event.cost_estimate_usd is not None:
                cost = (cost or Decimal("0")) + event.cost_estimate_usd
    for scored in scorer_results:
        judge_tokens, judge_usd = judge_cost(scored)
        tokens += judge_tokens
        if judge_usd is not None:
            cost = (cost or Decimal("0")) + judge_usd
    return tokens, cost


# --- aggregation --------------------------------------------------------------------


def _aggregate(
    spec: EvalSpec,
    target: EvalTarget,
    per_case: list[CaseResult],
    *,
    eval_run_id: RunId,
    spec_hash: str,
    eval_spec_ref: str,
    started_at: datetime,
    duration_ms: int,
    halted_reason: str | None,
) -> EvalRunResult:
    weights = {case.id: case.weight for case in spec.cases}
    runnable = [c for c in per_case if c.status != "skipped"]
    weight_total = sum(weights.get(c.case_id, 1.0) for c in runnable)
    score = (
        sum(weights.get(c.case_id, 1.0) * c.score for c in runnable) / weight_total
        if weight_total > 0
        else 0.0
    )

    tokens_total = sum(c.tokens for c in runnable)
    costs = [c.cost_usd for c in runnable if c.cost_usd is not None]
    cost_total = sum(costs, Decimal("0")) if costs else None

    metadata: dict[str, Any] = {
        "deterministic": spec.deterministic,
        "seed": spec.seed,
    }
    if halted_reason is not None:
        metadata["halted_reason"] = halted_reason
    if not isinstance(target, ToolEvalTarget):
        metadata["per_agent"] = {target.compiled.agent_name: score}

    return EvalRunResult(
        eval_run_id=eval_run_id,
        eval_name=spec.name,
        scope=spec.scope,
        eval_spec_ref=eval_spec_ref,
        eval_spec_hash=spec_hash,
        target_ref=target.ref,
        target_version=target.version,
        pin_set_hash=(
            "" if isinstance(target, ToolEvalTarget)
            else target.compiled.pin_set_hash
        ),
        started_at=started_at,
        completed_at=datetime.now(UTC),
        duration_ms=duration_ms,
        cases_total=len(per_case),
        cases_passed=sum(1 for c in runnable if c.pass_),
        cases_failed=sum(1 for c in runnable if not c.pass_),
        cases_skipped=sum(1 for c in per_case if c.status == "skipped"),
        score=score,
        threshold=spec.threshold,
        passed=score >= spec.threshold,
        per_case=per_case,
        per_scorer=_scorer_summaries(spec, runnable),
        cost_total_usd=cost_total,
        tokens_total=tokens_total,
        metadata=metadata,
    )


def _scorer_summaries(
    spec: EvalSpec, runnable: list[CaseResult]
) -> dict[str, ScorerSummary]:
    summaries: dict[str, ScorerSummary] = {}
    for config in spec.scorers:
        scores: list[float] = []
        passes = 0
        for case_result in runnable:
            for scored in case_result.scorer_results:
                if scored.scorer_name == config.name:
                    scores.append(scored.score)
                    passes += int(scored.pass_)
        if not scores:
            summaries[config.name] = ScorerSummary(scorer_name=config.name)
            continue
        summaries[config.name] = ScorerSummary(
            scorer_name=config.name,
            average_score=sum(scores) / len(scores),
            pass_rate=passes / len(scores),
            p50=_percentile(scores, 0.50),
            p95=_percentile(scores, 0.95),
        )
    return summaries


def _percentile(scores: list[float], q: float) -> float:
    """Nearest-rank percentile on the sorted scores."""
    ordered = sorted(scores)
    rank = max(0, min(len(ordered) - 1, round(q * (len(ordered) - 1))))
    return ordered[rank]


# --- artifacts ------------------------------------------------------------------------


def _safe_filename(case_id: str) -> str:
    return "".join(
        ch if ch.isalnum() or ch in "-_." else "_" for ch in case_id
    ) or "_"


def write_eval_artifact(
    result: EvalRunResult, *, project_dir: Path | None = None
) -> Path:
    """Persist the result under ``~/.foundry/runs/<eval_run_id>/`` (docs/40
    § Eval result lifecycle): ``eval_result.json`` + per-case detail under
    ``cases/``; project/agent evals also append one line to the project's
    ``.foundry/eval_history.jsonl``."""
    directory = run_dir(str(result.eval_run_id))
    cases_dir = directory / "cases"
    cases_dir.mkdir(parents=True, exist_ok=True)
    (directory / "eval_result.json").write_text(
        result.model_dump_json(indent=2, by_alias=True) + "\n"
    )
    for case_result in result.per_case:
        (cases_dir / f"{_safe_filename(case_result.case_id)}.json").write_text(
            case_result.model_dump_json(indent=2, by_alias=True) + "\n"
        )
    if project_dir is not None:
        history = project_dir / ".foundry" / "eval_history.jsonl"
        history.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "eval_run_id": str(result.eval_run_id),
            "eval_name": result.eval_name,
            "scope": result.scope,
            "target_ref": result.target_ref,
            "target_version": result.target_version,
            "eval_spec_hash": result.eval_spec_hash,
            "pin_set_hash": result.pin_set_hash,
            "score": result.score,
            "passed": result.passed,
            "completed_at": result.completed_at.isoformat(),
            "artifact_dir": str(directory),
        }
        with history.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")
    return directory


def load_eval_result(ref: str | Path) -> EvalRunResult:
    """Read a persisted EvalRunResult back — by eval_run_id (resolved under
    ``~/.foundry/runs/``), artifact directory, or direct file path. This is
    the Phase 6 read surface."""
    path = Path(ref)
    if path.is_dir():
        path = path / "eval_result.json"
    elif not path.exists():
        path = run_dir(str(ref)) / "eval_result.json"
    if not path.exists():
        raise ConfigLoadError(
            f"no eval result found for {str(ref)!r} (checked {path})",
            context={"ref": str(ref), "checked": str(path)},
        )
    return EvalRunResult.model_validate_json(path.read_text())


def list_eval_history(project_dir: Path) -> list[dict[str, Any]]:
    """The project's eval_history.jsonl entries, oldest first."""
    history = project_dir / ".foundry" / "eval_history.jsonl"
    if not history.exists():
        return []
    return [
        json.loads(line)
        for line in history.read_text().splitlines()
        if line.strip()
    ]


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


def _preview(value: Any, limit: int = 200) -> str:
    text = json.dumps(value, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


__all__ = [
    "AgentEvalTarget",
    "EvalTarget",
    "ProjectEvalTarget",
    "ToolEvalTarget",
    "list_eval_history",
    "load_eval_result",
    "load_tool_target",
    "run_eval",
    "validate_cases",
    "write_eval_artifact",
]
