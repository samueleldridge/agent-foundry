"""LangGraph runtime adapter — wire a CompiledSystem into a StateGraph and run it.

The ONLY module (with ``_langgraph_types``) allowed to import ``langgraph`` /
``langchain_core`` (import-boundary lint). Compilation lives in
``foundry.orchestration.compiler`` (re-exported here for callers); step
execution lives in ``foundry.runtime.execution``. This module only:

- expands the flow plan into StateGraph nodes/edges — the agent step is a
  sub-graph (``begin`` → ``llm`` ⇄ ``tools`` → ``finish``, plus
  ``turn``/``turn_end`` for memory agents) so the tool loop and the memory
  turn loop are checkpointed at every boundary;
- attaches the selected checkpointer (memory / sqlite / none) and resumes a
  thread whose last checkpoint still has pending nodes (kill → rerun with
  the same run id → completes);
- wraps every node in a ``foundry.node`` span and the run in ``foundry.run``
  (docs/01 § Observability event spec).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Hashable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.graph import END, START, StateGraph

from foundry.cache import (
    InProcessResultCache,
    default_result_cache_path,
)
from foundry.connections import InProcessConnectionPool
from foundry.core import (
    CacheBundle,
    ResultCache,
    RunCompleted,
    RunFailed,
    RunStarted,
    Session,
    WarningEvent,
)
from foundry.core.errors import FoundryError, OrchestrationError
from foundry.observability.tracing import (
    foundry_span,
    set_span_attributes,
    worker_id,
)
from foundry.orchestration.compiler import compile_project, compile_system
from foundry.retrieval import (
    MappingRetrieverAccessor,
    build_retriever_accessor,
)
from foundry.runtime._langgraph_types import GraphState, build_checkpointer
from foundry.runtime.checkpointers import default_checkpoint_db
from foundry.runtime.compiled import (
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

# --- graph wiring -----------------------------------------------------------------


def _wire_graph(
    graph: StateGraph[GraphState],
    compiled: CompiledProject,
    runtime: AgentStepRuntime,
    emitter: EventEmitter,
) -> None:
    """Expand the flow plan into nodes + edges. The agent step becomes a
    sub-graph whose boundaries are exactly the checkpoint boundaries."""
    agent = compiled.agent_name
    project_name = compiled.project.system.name
    run_id = str(runtime.session.run_id)
    reducers = compiled.compiled_state.reducers
    memory_mode = compiled.memory is not None

    def wrap(node_name: str, agent_name: str, fn: Any) -> Any:
        async def node(state: GraphState) -> dict[str, Any]:
            with foundry_span(
                "foundry.node",
                {
                    "run_id": run_id,
                    "project": project_name,
                    "node": node_name,
                    "agent": agent_name,
                },
            ):
                update: dict[str, Any] = await fn(
                    state.get("conv") or {}, state.get("state", {})
                )
                return update

        return node

    def make_function_step(function: CompiledFunction) -> Any:
        async def function_step(
            conv: dict[str, Any], run_state: dict[str, Any]
        ) -> dict[str, Any]:
            delta = await run_function_step(
                compiled, function, run_state, runtime.session, emitter
            )
            return {"state": apply_delta(run_state, delta, reducers)}

        return function_step

    # Agent sub-graph. The begin node carries the agent's flow-visible name;
    # the internal slices are suffixed (":" is reserved by LangGraph).
    llm_name = f"{agent}__llm"
    tools_name = f"{agent}__tools"
    finish_name = f"{agent}__finish"
    turn_name = f"{agent}__turn"
    turn_end_name = f"{agent}__turn_end"
    generated = {llm_name, tools_name, finish_name, turn_name, turn_end_name}
    taken = generated & set(compiled.functions)
    if taken:
        raise OrchestrationError(
            f"function node name(s) collide with the agent's internal "
            f"sub-node names: {', '.join(sorted(taken))}; rename the "
            f"function(s) (reserved: <agent>__llm/tools/finish/turn/turn_end)",
            context={"collisions": sorted(taken)},
        )

    graph.add_node(agent, wrap(agent, agent, runtime.begin))
    graph.add_node(llm_name, wrap(llm_name, agent, runtime.llm_round))
    graph.add_node(tools_name, wrap(tools_name, agent, runtime.dispatch_tools))
    graph.add_node(finish_name, wrap(finish_name, agent, runtime.finish))
    if memory_mode:
        graph.add_node(turn_name, wrap(turn_name, agent, runtime.start_turn))
        graph.add_node(
            turn_end_name, wrap(turn_end_name, agent, runtime.end_turn)
        )

    def conv_of(state: GraphState) -> dict[str, Any]:
        return state.get("conv") or {}

    begin_routes: dict[Hashable, str] = {"llm": llm_name, "finish": finish_name}
    llm_routes: dict[Hashable, str] = {"tools": tools_name, "finish": finish_name}
    if memory_mode:
        begin_routes["turn"] = turn_name
        llm_routes["turn_end"] = turn_end_name
    graph.add_conditional_edges(
        agent,
        lambda state: runtime.route_after_begin(conv_of(state)),
        begin_routes,
    )
    graph.add_conditional_edges(
        llm_name,
        lambda state: runtime.route_after_llm(conv_of(state)),
        llm_routes,
    )
    graph.add_edge(tools_name, llm_name)
    if memory_mode:
        graph.add_edge(turn_name, llm_name)
        graph.add_conditional_edges(
            turn_end_name,
            lambda state: runtime.route_after_turn_end(conv_of(state)),
            {"turn": turn_name, "finish": finish_name},
        )

    # Flow-level edges: single, or the Phase 2c sequential shape.
    if compiled.flow_steps:
        previous: Any = START
        for step in compiled.flow_steps:
            if step == agent:
                graph.add_edge(previous, agent)
                previous = finish_name
            else:
                graph.add_node(
                    step,
                    wrap(step, step, make_function_step(compiled.functions[step])),
                )
                graph.add_edge(previous, step)
                previous = step
        graph.add_edge(previous, END)
    else:
        graph.add_edge(START, agent)
        graph.add_edge(finish_name, END)


# --- execution --------------------------------------------------------------------


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
) -> RunResult:
    """Run the compiled system through a LangGraph StateGraph.

    The graph thread id is the run id. With a checkpointer attached, a
    thread whose last checkpoint still has pending nodes RESUMES from that
    checkpoint (``input_data`` is ignored); otherwise a fresh run starts.
    ``event_sink`` receives every RunEvent synchronously as it happens —
    this is the streaming surface (docs/10 § Streaming events).
    """
    emitter = EventEmitter(session, event_sink, start_sequence=start_sequence)
    started = datetime.now(UTC)
    counters = RunCounters()
    pool = pool or InProcessConnectionPool()
    retriever_accessor: MappingRetrieverAccessor | None = None
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

    runtime = AgentStepRuntime(compiled, session, emitter, pool, counters)
    graph: StateGraph[GraphState] = StateGraph(GraphState)
    _wire_graph(graph, compiled, runtime, emitter)

    saver = build_checkpointer(
        checkpointer,
        checkpoint_db or default_checkpoint_db(compiled.project.system.name),
    )
    app = graph.compile(checkpointer=saver)
    config: RunnableConfig = {"configurable": {"thread_id": str(session.run_id)}}

    resumed = False
    if saver is not None:
        snapshot = await app.aget_state(config)
        # Pending nodes on the last committed checkpoint == an interrupted
        # run (Phase 3 exit gate: kill mid-run, rerun same id, completes).
        resumed = bool(snapshot.next)

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
            if compiled.retrievers:
                retriever_accessor, retriever_conn_accessors = (
                    await build_retriever_accessor(
                        compiled.retrievers,
                        pool=pool,
                        project=compiled.project.system.name,
                        project_dir=compiled.project.directory,
                        agent_name=compiled.agent_name,
                        secrets=compiled.secrets,
                        transport=compiled.transport,
                        emit=emitter.emit,
                    )
                )
                runtime.retrievers = retriever_accessor
            graph_input = (
                None
                if resumed
                else cast(
                    GraphState, {"state": seed_state(compiled, input_data)}
                )
            )
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
        # Sequential flows: the pipeline's product IS the final state (a
        # post-agent function may have transformed the agent's output).
        output = final_state if compiled.flow_steps else final.get("output")

        duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
        last_response = counters.last_response
        usage = last_response.usage if last_response else None
        emitter.emit(
            RunCompleted,
            status="success",
            final_output=output,
            total_input_tokens=usage.input_tokens if usage else 0,
            total_output_tokens=usage.output_tokens if usage else 0,
            total_cost_estimate_usd=(
                last_response.cost_estimate_usd if last_response else None
            ),
            duration_ms=duration_ms,
        )
        set_span_attributes(
            run_span,
            {
                "status": "success",
                "total_duration_ms": duration_ms,
                "total_input_tokens": usage.input_tokens if usage else 0,
                "total_output_tokens": usage.output_tokens if usage else 0,
                "total_cost_estimate_usd": (
                    last_response.cost_estimate_usd if last_response else None
                ),
            },
        )
    return RunResult(
        output=output,
        response=last_response,
        pool_metrics=pool_metrics,
        llm_call_count=counters.llm_call_count,
        final_state=final_state,
        resumed=resumed,
    )


async def _release_retrievers(accessors: list[Any]) -> None:
    for accessor in accessors:
        await accessor.release_all()


__all__ = [
    "CompileWarning",
    "CompiledFunction",
    "CompiledProject",
    "RunResult",
    "compile_project",
    "compile_system",
    "run_project",
]
