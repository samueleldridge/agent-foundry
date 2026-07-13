"""LangGraph runtime adapter — wire a CompiledSystem into a StateGraph and run it.

The ONLY module (with ``_langgraph_types``) allowed to import ``langgraph`` /
``langchain_core`` (import-boundary lint). Compilation lives in
``foundry.orchestration.compiler`` (re-exported here for callers); step
execution lives in ``foundry.runtime.execution``. This module only:

- expands the FlowPlan tree into StateGraph nodes/edges — every agent is a
  sub-graph (``begin`` → ``llm`` ⇄ ``tools`` → ``finish``, plus
  ``turn``/``turn_end`` for memory agents); supervisor/graph patterns add
  router nodes (``__dispatch`` / ``__handoff`` / ``__route``) that emit
  ``handoff`` events, count hops, and enforce max-hops policy; parallel
  patterns fan out from an ``__enter`` node and join on a waiting edge;
- PROJECTS state per node before invocation — an agent's slices receive
  only their declared read fields (structural visibility, docs/22);
- converts ``ApprovalRequired`` into a LangGraph ``interrupt()`` at the
  node boundary (docs/32): the checkpointer persists the pending payload,
  the run returns ``status="approval_pending"``, and a later
  ``approval_response`` resumes via ``Command(resume=...)``;
- attaches the selected checkpointer (memory / sqlite / none) and resumes a
  thread whose last checkpoint still has pending nodes;
- wraps every node in a ``foundry.node`` span and the run in ``foundry.run``.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Hashable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from foundry.cache import (
    InProcessResultCache,
    default_result_cache_path,
)
from foundry.connections import InProcessConnectionPool
from foundry.core import (
    ApprovalRequiredEvent,
    ApprovalResolved,
    CacheBundle,
    Handoff,
    ResultCache,
    RunCompleted,
    RunFailed,
    RunStarted,
    Session,
    StateTransition,
    WarningEvent,
)
from foundry.core.errors import (
    ApprovalRequired,
    CompileError,
    FoundryError,
    MaxHopsExceededError,
    OrchestrationError,
)
from foundry.observability.tracing import (
    foundry_span,
    set_span_attributes,
    worker_id,
)
from foundry.orchestration.compiler import compile_project, compile_system
from foundry.orchestration.hitl import (
    RUN_STATUS_APPROVAL_PENDING,
    InterruptPayload,
    interrupt_payload,
    parse_payload,
    parse_resolution,
    resolution_record,
)
from foundry.orchestration.patterns import (
    END_SENTINEL,
    FlowPlan,
    GraphPlan,
    LeafNode,
    ParallelPlan,
    PlanNode,
    SequentialPlan,
    SupervisorPlan,
)
from foundry.retrieval import (
    build_retriever_accessor,
)
from foundry.runtime._langgraph_types import (
    GraphState,
    build_checkpointer,
    make_graph_state,
)
from foundry.runtime.checkpointers import (
    default_checkpoint_db,
    graph_schema_fingerprint,
)
from foundry.runtime.compiled import (
    CompiledAgent,
    CompiledFunction,
    CompiledProject,
    CompileWarning,
    RunResult,
)
from foundry.runtime.execution import (
    AgentStepRuntime,
    EventEmitter,
    EventSink,
    RunCounters,
    apply_delta,
    run_function_step,
    seed_state,
)

FLOW_ROOT = "flow_root"
"""Owner name for the top-level flow's synthetic nodes (enter/exit)."""


def _collect_owners(node: PlanNode) -> list[str]:
    """Routing owners (supervisors + worker targets + graph nodes) that
    need namespaced route/decision channels."""
    owners: list[str] = []
    if isinstance(node, SupervisorPlan):
        owners.append(node.supervisor)
        for target, plan in node.workers:
            owners.append(target)
            owners.extend(_collect_owners(plan))
    elif isinstance(node, SequentialPlan):
        for step in node.steps:
            owners.extend(_collect_owners(step))
    elif isinstance(node, ParallelPlan):
        for branch in node.branches:
            owners.extend(_collect_owners(branch))
        for step in node.then:
            owners.extend(_collect_owners(step))
    elif isinstance(node, GraphPlan):
        owners.extend(node.edges_by_source)
    return owners


class _Wiring:
    """Recursive FlowPlan → StateGraph expansion. ``wire`` returns the
    subtree's (entry node, exit nodes); the caller connects them."""

    def __init__(
        self,
        graph: Any,
        compiled: CompiledProject,
        runtimes: dict[str, AgentStepRuntime],
        emitter: EventEmitter,
        session: Session,
    ) -> None:
        self.graph = graph
        self.compiled = compiled
        self.runtimes = runtimes
        self.emitter = emitter
        self.session = session
        self.guardrails = compiled.project.system.guardrails
        self.project_name = compiled.project.system.name
        self.run_id = str(session.run_id)

    # --- node wrappers -----------------------------------------------------

    def wrap(
        self,
        node_name: str,
        agent_name: str,
        fn: Any,
        *,
        runtime: AgentStepRuntime | None = None,
        view: Any = None,
    ) -> Any:
        """Span + state projection + HITL interrupt conversion around one
        node-sized slice. ``fn(conv, scoped_state) -> update``."""
        conv_channel = f"conv__{agent_name}" if runtime is not None else None
        route_channel = f"route__{agent_name}"

        async def node(state: GraphState) -> dict[str, Any]:
            with foundry_span(
                "foundry.node",
                {
                    "run_id": self.run_id,
                    "project": self.project_name,
                    "node": node_name,
                    "agent": agent_name,
                },
            ):
                full_state: dict[str, Any] = state.get("state") or {}
                # STRUCTURAL visibility: the slice receives the agent's
                # read projection — forbidden fields are absent, not
                # None-ed (docs/22; Phase 7 exit gate).
                scoped = (
                    view.project_input(full_state)
                    if view is not None
                    else dict(full_state)
                )
                conv: dict[str, Any] = (
                    (state.get(conv_channel) or {}) if conv_channel else {}
                )
                resolved: dict[str, dict[str, Any]] = dict(
                    state.get("approvals") or {}
                )
                new_resolutions: dict[str, Any] = {}
                if runtime is not None:
                    runtime.approvals = resolved
                while True:
                    try:
                        update: dict[str, Any] = await fn(conv, scoped)
                        break
                    except ApprovalRequired as pending:
                        if pending.approval_id in resolved:
                            raise OrchestrationError(
                                "non-idempotent approval flow: approval "
                                f"{pending.approval_id!r} was resolved "
                                f"({resolved[pending.approval_id]['decision']})"
                                " but the handler raised it again — after "
                                "resolution the handler must check "
                                "ctx.approval_resolved() instead of "
                                "re-raising (docs/32 § Re-execution)",
                                context={
                                    "approval_id": pending.approval_id,
                                    "agent": agent_name,
                                },
                            ) from pending
                        payload = interrupt_payload(
                            pending,
                            agent_name=agent_name,
                            tool_ref=pending.approval_context.get("tool_ref"),
                        )
                        try:
                            resume_value = interrupt(payload)
                        except GraphInterrupt:
                            # Actually pausing (not a replay): audit it.
                            self.emitter.emit(
                                ApprovalRequiredEvent,
                                agent_name=agent_name,
                                approval_id=pending.approval_id,
                                prompt=pending.prompt,
                                context=pending.approval_context,
                            )
                            raise
                        record = parse_resolution(resume_value).model_dump(
                            mode="json"
                        )
                        resolved[pending.approval_id] = record
                        new_resolutions[pending.approval_id] = record
                        if runtime is not None:
                            runtime.approvals = resolved
                state_delta: dict[str, Any] | None = update.get("state")
                if runtime is not None and state_delta:
                    # docs/80 § flow control: every agent state mutation
                    # emits state.transition (function nodes carry the same
                    # fields on function_node.completed instead).
                    self.emitter.emit(
                        StateTransition,
                        agent_name=agent_name,
                        fields_written=sorted(state_delta),
                        bytes_delta=len(
                            json.dumps(state_delta, default=str).encode()
                        ),
                    )
                return self._translate(
                    update, conv_channel, route_channel, new_resolutions
                )

        return node

    @staticmethod
    def _translate(
        update: dict[str, Any],
        conv_channel: str | None,
        route_channel: str,
        new_resolutions: dict[str, Any],
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, value in update.items():
            if key == "conv" and conv_channel is not None:
                out[conv_channel] = value
            elif key == "route":
                out[route_channel] = value
            else:
                out[key] = value
        if new_resolutions:
            out["approvals"] = new_resolutions
        return out

    def noop(self, node_name: str) -> str:
        async def passthrough(state: GraphState) -> dict[str, Any]:
            return {}

        self.graph.add_node(node_name, passthrough)
        return node_name

    def _connect(self, sources: tuple[str, ...], target: str) -> None:
        """Edge(s) into ``target``; multiple sources become a WAITING edge
        (target runs once ALL sources have completed — the fan-in)."""
        if len(sources) == 1:
            self.graph.add_edge(sources[0], target)
        else:
            self.graph.add_edge(list(sources), target)

    # --- agent / function leaves -------------------------------------------

    def wire_agent(self, name: str) -> tuple[str, tuple[str, ...]]:
        runtime = self.runtimes[name]
        view = runtime.view
        llm_name = f"{name}__llm"
        tools_name = f"{name}__tools"
        finish_name = f"{name}__finish"
        turn_name = f"{name}__turn"
        turn_end_name = f"{name}__turn_end"
        memory_mode = runtime.ca.memory is not None

        add = self.graph.add_node
        add(name, self.wrap(name, name, runtime.begin,
                            runtime=runtime, view=view))
        add(llm_name, self.wrap(llm_name, name, runtime.llm_round,
                                runtime=runtime, view=view))
        add(tools_name, self.wrap(tools_name, name, runtime.dispatch_tools,
                                  runtime=runtime, view=view))
        add(finish_name, self.wrap(finish_name, name, runtime.finish,
                                   runtime=runtime, view=view))
        if memory_mode:
            add(turn_name, self.wrap(turn_name, name, runtime.start_turn,
                                     runtime=runtime, view=view))
            add(turn_end_name, self.wrap(turn_end_name, name,
                                         runtime.end_turn,
                                         runtime=runtime, view=view))

        conv_channel = f"conv__{name}"

        def conv_of(state: GraphState) -> dict[str, Any]:
            return state.get(conv_channel) or {}

        begin_routes: dict[Hashable, str] = {
            "llm": llm_name, "finish": finish_name,
        }
        llm_routes: dict[Hashable, str] = {
            "tools": tools_name, "finish": finish_name,
        }
        if memory_mode:
            begin_routes["turn"] = turn_name
            llm_routes["turn_end"] = turn_end_name
        self.graph.add_conditional_edges(
            name,
            lambda state: runtime.route_after_begin(conv_of(state)),
            begin_routes,
        )
        self.graph.add_conditional_edges(
            llm_name,
            lambda state: runtime.route_after_llm(conv_of(state)),
            llm_routes,
        )
        if runtime.handoff_targets:
            # A recorded worker handoff short-circuits the loop.
            self.graph.add_conditional_edges(
                tools_name,
                lambda state: runtime.route_after_tools(conv_of(state)),
                {"llm": llm_name, "finish": finish_name},
            )
        else:
            self.graph.add_edge(tools_name, llm_name)
        if memory_mode:
            self.graph.add_edge(turn_name, llm_name)
            self.graph.add_conditional_edges(
                turn_end_name,
                lambda state: runtime.route_after_turn_end(conv_of(state)),
                {"turn": turn_name, "finish": finish_name},
            )
        return name, (finish_name,)

    def wire_function(self, name: str) -> tuple[str, tuple[str, ...]]:
        function = self.compiled.functions[name]

        async def function_step(
            conv: dict[str, Any], run_state: dict[str, Any]
        ) -> dict[str, Any]:
            delta = await run_function_step(
                self.compiled, function, run_state, self.session, self.emitter
            )
            return {"state": delta}

        view = self.compiled.compiled_state.agent_views[name]
        self.graph.add_node(
            name, self.wrap(name, name, function_step, view=view)
        )
        return name, (name,)

    # --- recursive pattern wiring --------------------------------------------

    def wire(self, node: PlanNode) -> tuple[str, tuple[str, ...]]:
        if isinstance(node, LeafNode):
            if node.kind == "agent":
                return self.wire_agent(node.name)
            return self.wire_function(node.name)
        if isinstance(node, SequentialPlan):
            return self.wire_sequential(node)
        if isinstance(node, ParallelPlan):
            return self.wire_parallel(node)
        if isinstance(node, SupervisorPlan):
            return self.wire_supervisor(node)
        return self.wire_graph_pattern(node)

    def wire_sequential(
        self, plan: SequentialPlan
    ) -> tuple[str, tuple[str, ...]]:
        entry: str | None = None
        exits: tuple[str, ...] = ()
        for step in plan.steps:
            step_entry, step_exits = self.wire(step)
            if entry is None:
                entry = step_entry
            else:
                self._connect(exits, step_entry)
            exits = step_exits
        assert entry is not None  # schema: steps min_length=1
        return entry, exits

    def wire_parallel(
        self, plan: ParallelPlan
    ) -> tuple[str, tuple[str, ...]]:
        owner = plan.name or FLOW_ROOT
        enter = self.noop(f"{owner}__enter")
        branch_exits: list[str] = []
        for branch in plan.branches:
            branch_entry, exits = self.wire(branch)
            self.graph.add_edge(enter, branch_entry)
            branch_exits.extend(exits)
        current_exits: tuple[str, ...] = tuple(branch_exits)
        if plan.join is not None:
            join_entry, join_exits = self.wire(plan.join)
            self._connect(current_exits, join_entry)
            current_exits = join_exits
        for step in plan.then:
            step_entry, step_exits = self.wire(step)
            self._connect(current_exits, step_entry)
            current_exits = step_exits
        return enter, current_exits

    def wire_supervisor(
        self, plan: SupervisorPlan
    ) -> tuple[str, tuple[str, ...]]:
        sup = plan.supervisor
        owner = plan.name or sup
        sup_entry, sup_exits = self.wire_agent(sup)
        exit_node = self.noop(f"{owner}__exit")
        dispatch_node = f"{sup}__dispatch"
        self.graph.add_node(dispatch_node, self._make_dispatch(plan))
        self._connect(sup_exits, dispatch_node)

        worker_entries: dict[str, str] = {}
        for target, worker_plan in plan.workers:
            worker_entry, worker_exits = self.wire(worker_plan)
            worker_entries[target] = worker_entry
            handoff_node = f"{target}__handoff"
            self.graph.add_node(
                handoff_node, self._make_worker_handoff(plan, target)
            )
            self._connect(worker_exits, handoff_node)
            worker_decision = f"decision__{target}"
            self.graph.add_conditional_edges(
                handoff_node,
                lambda state, ch=worker_decision: state[ch],
                {"supervisor": sup_entry, END_SENTINEL: exit_node},
            )

        dispatch_map: dict[Hashable, str] = {
            target: worker_entries[target]
            for target in plan.supervisor_targets
            if target != END_SENTINEL
        }
        dispatch_map[END_SENTINEL] = exit_node
        sup_decision = f"decision__{sup}"
        self.graph.add_conditional_edges(
            dispatch_node,
            lambda state, ch=sup_decision: state[ch],
            dispatch_map,
        )
        return sup_entry, (exit_node,)

    def _check_global_hops(self, hops: int) -> None:
        if hops + 1 > self.guardrails.max_hops:
            raise MaxHopsExceededError(
                f"run exceeded guardrails.max_hops "
                f"({self.guardrails.max_hops} edge traversals); the flow "
                "is looping beyond the project-level safety net",
                context={"max_hops": self.guardrails.max_hops},
            )

    def _make_dispatch(self, plan: SupervisorPlan) -> Any:
        sup = plan.supervisor
        route_channel = f"route__{sup}"
        decision_channel = f"decision__{sup}"

        async def dispatch(state: GraphState) -> dict[str, Any]:
            updates: dict[str, Any] = {"hops": 1}
            hops = int(state.get("hops") or 0)
            run_state: dict[str, Any] = state.get("state") or {}
            route = state.get(route_channel)
            trigger = "end"
            decision = END_SENTINEL
            if (
                plan.termination_when is not None
                and plan.termination_when.evaluate(run_state)
            ):
                trigger = "rule"
            elif route in (None, END_SENTINEL):
                trigger = "end"
            else:
                self._check_global_hops(hops)
                if hops + 1 > plan.max_hops:
                    if plan.on_max_hops == "error":
                        raise MaxHopsExceededError(
                            f"supervisor {sup!r} exceeded "
                            f"termination.max_hops ({plan.max_hops}); "
                            "on_max_hops: error",
                            context={"supervisor": sup,
                                     "max_hops": plan.max_hops},
                        )
                    if plan.on_max_hops == "return_partial" or state.get(
                        "escalated"
                    ):
                        # return_partial — or the escalation was already
                        # spent; end with what we have.
                        if plan.on_max_hops == "return_partial":
                            updates["flow_status"] = "max_hops"
                        decision, trigger = END_SENTINEL, "end"
                    else:  # escalate: one forced final handoff
                        assert plan.escalate_to is not None
                        decision, trigger = plan.escalate_to, "rule"
                        updates["escalated"] = True
                else:
                    decision, trigger = str(route), "llm"
            self.emitter.emit(
                Handoff,
                from_agent=sup,
                to_agent=decision,
                trigger=trigger,
                hop_number=hops + 1,
            )
            updates[decision_channel] = decision
            return updates

        return dispatch

    def _make_worker_handoff(
        self, plan: SupervisorPlan, target: str
    ) -> Any:
        decision_channel = f"decision__{target}"

        async def handoff(state: GraphState) -> dict[str, Any]:
            hops = int(state.get("hops") or 0)
            run_state: dict[str, Any] = state.get("state") or {}
            decision = "supervisor"
            if state.get("escalated") and target == plan.escalate_to:
                # The forced final handoff: escalation worker → END.
                decision = END_SENTINEL
            elif (
                plan.termination_when is not None
                and target in plan.workers_may_end
                and plan.termination_when.evaluate(run_state)
            ):
                decision = END_SENTINEL
            else:
                self._check_global_hops(hops)
            self.emitter.emit(
                Handoff,
                from_agent=target,
                to_agent=(
                    plan.supervisor
                    if decision == "supervisor"
                    else END_SENTINEL
                ),
                trigger="rule" if decision == "supervisor" else "end",
                hop_number=hops + 1,
            )
            return {decision_channel: decision, "hops": 1}

        return handoff

    def wire_graph_pattern(
        self, plan: GraphPlan
    ) -> tuple[str, tuple[str, ...]]:
        owner = plan.name or FLOW_ROOT
        exit_node = self.noop(f"{owner}__exit")
        entries: dict[str, str] = {}
        exits_map: dict[str, tuple[str, ...]] = {}
        for leaf in plan.nodes:
            entry, exits = self.wire(leaf)
            entries[leaf.name] = entry
            exits_map[leaf.name] = exits
        for source, edges in plan.edges_by_source.items():
            router = f"{source}__route"
            self.graph.add_node(
                router, self._make_graph_router(source, edges)
            )
            self._connect(exits_map[source], router)
            path_map: dict[Hashable, str] = {
                edge.to: entries[edge.to]
                for edge in edges
                if edge.to != END_SENTINEL
            }
            path_map[END_SENTINEL] = exit_node
            decision_channel = f"decision__{source}"
            self.graph.add_conditional_edges(
                router,
                lambda state, ch=decision_channel: state[ch],
                path_map,
            )
        return entries[plan.start], (exit_node,)

    def _make_graph_router(self, source: str, edges: Any) -> Any:
        decision_channel = f"decision__{source}"

        async def route(state: GraphState) -> dict[str, Any]:
            hops = int(state.get("hops") or 0)
            self._check_global_hops(hops)
            run_state: dict[str, Any] = state.get("state") or {}
            target: str | None = None
            for edge in edges:
                if edge.predicate is None or edge.predicate.evaluate(
                    run_state
                ):
                    target = edge.to
                    break
            if target is None:
                raise OrchestrationError(
                    f"no edge predicate matched leaving graph node "
                    f"{source!r}; predicates: "
                    + "; ".join(
                        e.predicate.source if e.predicate else "<else>"
                        for e in edges
                    )
                    + " — add an unconditional else-edge or fix the "
                    "predicate cover (docs/30 § graph)",
                    context={"node": source},
                )
            self.emitter.emit(
                Handoff,
                from_agent=source,
                to_agent=target,
                trigger="end" if target == END_SENTINEL else "rule",
                hop_number=hops + 1,
            )
            return {decision_channel: target, "hops": 1}

        return route


def _wire_flow(wiring: _Wiring, plan: FlowPlan) -> None:
    """Expand the whole plan and connect it to START/END."""
    entry, exits = wiring.wire(plan.root)
    wiring.graph.add_edge(START, entry)
    for exit_node in exits:
        wiring.graph.add_edge(exit_node, END)


# --- execution --------------------------------------------------------------------


def _pending_interrupts(final: dict[str, Any]) -> list[InterruptPayload]:
    payloads: list[InterruptPayload] = []
    for intr in final.get("__interrupt__") or ():
        parsed = parse_payload(getattr(intr, "value", None))
        if parsed is not None:
            payloads.append(parsed)
    return payloads


async def run_project(
    compiled: CompiledProject,
    input_data: dict[str, Any],
    session: Session,
    event_sink: EventSink | None = None,
    *,
    pool: InProcessConnectionPool | None = None,
    checkpointer: str = "none",
    checkpoint_db: Path | None = None,
    start_sequence: int = 0,
    approval_response: dict[str, Any] | None = None,
) -> RunResult:
    """Run the compiled system through a LangGraph StateGraph.

    The graph thread id is the run id. With a checkpointer attached, a
    thread whose last checkpoint still has pending nodes RESUMES from that
    checkpoint (``input_data`` is ignored). ``approval_response``
    (``{"approval_id", "decision", "reason"}``) resolves a pending HITL
    approval on that thread and continues the run (docs/32 § Resume).
    ``event_sink`` receives every RunEvent synchronously as it happens.
    """
    emitter = EventEmitter(session, event_sink, start_sequence=start_sequence)
    started = datetime.now(UTC)
    counters = RunCounters()
    pool = pool or InProcessConnectionPool()
    retriever_conn_accessors: list[Any] = []

    # Session-scoped cache bundle (docs/10 § CacheAccessor on Session): the
    # dispatcher reads tool_result; the semantic side is driven per step.
    result_cache: ResultCache | None = None
    if compiled.uses_tool_cache:
        result_cache = InProcessResultCache(default_result_cache_path())
    if result_cache is not None or compiled.semantic_cache is not None:
        session = session.model_copy(
            update={
                "cache": CacheBundle(
                    semantic=(
                        compiled.semantic_cache.backend
                        if compiled.semantic_cache is not None
                        else None
                    ),
                    tool_result=result_cache,
                )
            }
        )

    plan = compiled.flow_plan()
    agent_map = compiled.agent_map()
    runtimes = {
        name: AgentStepRuntime(compiled, session, emitter, pool, counters,
                               agent=ca)
        for name, ca in agent_map.items()
    }

    reducers = compiled.compiled_state.reducers

    def state_merger(
        current: dict[str, Any], incoming: dict[str, Any]
    ) -> dict[str, Any]:
        return apply_delta(current or {}, incoming or {}, reducers)

    state_schema = make_graph_state(
        sorted(agent_map),
        sorted(set(_collect_owners(plan.root))),
        state_merger,
    )
    # The schema type is built at runtime; StateGraph's type parameters
    # cannot express it statically.
    graph: Any = StateGraph(cast(Any, state_schema))
    wiring = _Wiring(graph, compiled, runtimes, emitter, session)
    _wire_flow(wiring, plan)

    # The fingerprint binds persisted checkpoints to THIS compile's channel
    # set; resuming a checkpoint written under an older graph schema fails
    # loudly instead of silently rehydrating stale channels (Phase 7
    # review finding 4).
    saver = build_checkpointer(
        checkpointer,
        checkpoint_db or default_checkpoint_db(compiled.project.system.name),
        schema_fingerprint=graph_schema_fingerprint(
            state_schema.__annotations__
        ),
    )
    if approval_response is not None and saver is None:
        raise CompileError(
            "resuming an approval requires a persistent checkpointer; "
            "run with --checkpoint sqlite (docs/32: the pending state "
            "lives in the checkpointer)",
            context={"checkpointer": checkpointer},
        )
    app = graph.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(session.run_id)}}

    resumed = False
    if saver is not None:
        snapshot = await app.aget_state(config)
        # Pending nodes on the last committed checkpoint == an interrupted
        # run (Phase 3 exit gate: kill mid-run, rerun same id, completes).
        resumed = bool(snapshot.next)

    graph_input: Any
    if approval_response is not None:
        if not resumed:
            raise OrchestrationError(
                f"run {session.run_id} has no pending work to resume; "
                "nothing is awaiting approval",
                context={"run_id": str(session.run_id)},
            )
        emitter.emit(
            ApprovalResolved,
            approval_id=str(approval_response["approval_id"]),
            decision=approval_response["decision"],
            reason=approval_response.get("reason"),
        )
        graph_input = Command(
            resume=resolution_record(
                approval_response["decision"],
                approval_response.get("reason"),
            )
        )
    elif resumed:
        graph_input = None
    else:
        graph_input = cast(
            GraphState, {"state": seed_state(compiled, input_data)}
        )

    inputs_hash = hashlib.sha256(
        json.dumps(input_data, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]

    with foundry_span(
        "foundry.run",
        {
            "run_id": str(session.run_id),
            "project": compiled.project.system.name,
            "system_version": compiled.system_version,
            "pin_set_hash": compiled.pin_set_hash,
            "started_at": started.isoformat(),
            "worker_id": worker_id(),
            "resumed": resumed,
        },
    ) as run_span:
        emitter.emit(
            RunStarted,
            project=compiled.project.system.name,
            system_version=compiled.system_version,
            pin_set_hash=compiled.pin_set_hash,
            inputs_hash=inputs_hash,
        )
        for compile_warning in compiled.compile_warnings:
            emitter.emit(
                WarningEvent,
                agent_name=compile_warning.agent_name,
                category=compile_warning.category,
                message=compile_warning.message,
                error_class=None,
            )
        pool_metrics: dict[str, Any] = {}
        try:
            for name, ca in agent_map.items():
                if not ca.retrievers:
                    continue
                accessor, conn_accessors = await build_retriever_accessor(
                    ca.retrievers,
                    pool=pool,
                    project=compiled.project.system.name,
                    project_dir=compiled.project.directory,
                    agent_name=name,
                    secrets=compiled.secrets,
                    transport=compiled.transport,
                    emit=emitter.emit,
                )
                runtimes[name].retrievers = accessor
                retriever_conn_accessors.extend(conn_accessors)
            final = await app.ainvoke(
                graph_input, config if saver is not None else None
            )
        except FoundryError as exc:
            set_span_attributes(
                run_span,
                {"status": "failed", "error_class": type(exc).__name__},
            )
            emitter.emit(RunFailed, error=exc.to_dict())
            raise
        except Exception as exc:  # wrap: no arbitrary exceptions cross the boundary
            wrapped = OrchestrationError(
                f"run failed with an unclassified error: {exc}",
                context={"project": compiled.project.system.name},
                cause=exc,
            )
            set_span_attributes(
                run_span,
                {"status": "failed", "error_class": type(wrapped).__name__},
            )
            emitter.emit(RunFailed, error=wrapped.to_dict())
            raise wrapped from exc
        finally:
            pool_metrics = pool.metrics_snapshot()
            await _release_retrievers(retriever_conn_accessors)
            await pool.close_all()
            close = getattr(saver, "close", None)
            if close is not None:
                close()

        final_state: dict[str, Any] = final.get("state", {})
        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        last_response = counters.last_response

        pending = _pending_interrupts(final)
        if pending:
            # HITL pause: the checkpointer holds the durable pending state;
            # the process is free (docs/32 § Pause sequence).
            emitter.emit(
                RunCompleted,
                status=RUN_STATUS_APPROVAL_PENDING,
                final_output=None,
                total_input_tokens=counters.total_input_tokens,
                total_output_tokens=counters.total_output_tokens,
                total_cost_estimate_usd=counters.total_cost_estimate_usd,
                duration_ms=duration_ms,
            )
            set_span_attributes(
                run_span, {"status": RUN_STATUS_APPROVAL_PENDING}
            )
            return RunResult(
                output=None,
                response=last_response,
                pool_metrics=pool_metrics,
                llm_call_count=counters.llm_call_count,
                final_state=final_state,
                resumed=resumed,
                status=RUN_STATUS_APPROVAL_PENDING,
                pending_approval=pending[0].model_dump(mode="json"),
            )

        # Sequential flows: the pipeline's product IS the final state (a
        # post-agent function may have transformed the agent's output).
        output = final_state if compiled.flow_steps else final.get("output")
        status = (
            "max_hops" if final.get("flow_status") == "max_hops" else "success"
        )

        # Totals accumulate across EVERY LLM call in the run.
        emitter.emit(
            RunCompleted,
            status=status,
            final_output=output,
            total_input_tokens=counters.total_input_tokens,
            total_output_tokens=counters.total_output_tokens,
            total_cost_estimate_usd=counters.total_cost_estimate_usd,
            duration_ms=duration_ms,
        )
        set_span_attributes(
            run_span,
            {
                "status": status,
                "total_duration_ms": duration_ms,
                "total_input_tokens": counters.total_input_tokens,
                "total_output_tokens": counters.total_output_tokens,
                "total_cost_estimate_usd": counters.total_cost_estimate_usd,
            },
        )
    return RunResult(
        output=output,
        response=last_response,
        pool_metrics=pool_metrics,
        llm_call_count=counters.llm_call_count,
        final_state=final_state,
        resumed=resumed,
        status=status,
    )


async def _release_retrievers(accessors: list[Any]) -> None:
    for accessor in accessors:
        await accessor.release_all()


__all__ = [
    "CompileWarning",
    "CompiledAgent",
    "CompiledFunction",
    "CompiledProject",
    "RunResult",
    "compile_project",
    "compile_system",
    "run_project",
]
