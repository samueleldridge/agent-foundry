"""LangGraph runtime adapter — compile + run single/sequential flows.

The ONLY module (with ``_langgraph_types``) allowed to import ``langgraph`` /
``langchain_core`` (import-boundary lint). Phase 2 scope: compile a
``SystemSpec`` with a ``single`` flow (one agent node) or a ``sequential``
flow (function nodes + exactly one agent node, chained) into a ``StateGraph``.
Step execution lives in ``foundry.runtime.execution``; checkpointers,
streaming, multi-agent flows and the real compiler
(``foundry.orchestration.compiler``) land in Phase 3 — this module
deliberately stays a thin adapter.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from foundry.cache import (
    InProcessResultCache,
    default_result_cache_path,
    prepare_semantic_cache,
)
from foundry.catalog.loader import load_tool_version
from foundry.config import (
    ArtifactRef,
    EnvSecretsProvider,
    FoundryRoots,
    LoadedFunction,
    LoadedProject,
    SequentialFlow,
    SingleFlow,
    StateVisibility,
    ToolSpec,
    load_project,
)
from foundry.config.secrets import SecretsProvider
from foundry.connections import (
    InProcessConnectionPool,
    prepare_connections,
    validate_tool_connection_wiring,
)
from foundry.core import (
    CacheBundle,
    RegisteredTool,
    ResultCache,
    RetryPolicy,
    RunCompleted,
    RunFailed,
    RunStarted,
    Session,
    ToolDescriptor,
    ToolRegistry,
    WarningEvent,
)
from foundry.core.errors import (
    CompileError,
    FoundryError,
    OrchestrationError,
    ProviderConfigError,
    StateVisibilityError,
)
from foundry.memory import prepare_memory
from foundry.orchestration.state_scope import CompiledState, compile_state
from foundry.providers import resolve
from foundry.retrieval import (
    MappingRetrieverAccessor,
    build_retriever_accessor,
    prepare_retrievers,
)
from foundry.runtime._langgraph_types import GraphState
from foundry.runtime.compiled import (
    CompiledFunction,
    CompiledProject,
    CompileWarning,
    FunctionHandler,
    RunResult,
)
from foundry.runtime.execution import (
    EventEmitter,
    EventSink,
    RunCounters,
    apply_delta,
    run_agent_step,
    run_function_step,
    seed_state,
)

_OVERRIDABLE_SETTINGS = ("timeout_s", "retry_policy")

_EXECUTABLE_FLOWS = ("single", "sequential")


# --- flow validation ----------------------------------------------------------------


def _flow_node_refs(flow: Any) -> list[tuple[str, str]]:
    """Every node name a flow references, as (json_pointer, name) pairs.
    Works across all five flow types so reference resolution is validated
    even for patterns whose EXECUTION lands in Phase 3+."""
    refs: list[tuple[str, str]] = []
    if flow.type == "single":
        refs.append(("/flow/agent", flow.agent))
    elif flow.type == "sequential":
        refs.extend(
            (f"/flow/steps/{i}", step) for i, step in enumerate(flow.steps)
        )
    elif flow.type == "parallel":
        refs.extend(
            (f"/flow/parallel_branches/{i}", branch)
            for i, branch in enumerate(flow.parallel_branches)
        )
        if flow.join is not None:
            refs.append(("/flow/join", flow.join))
        refs.extend((f"/flow/then/{i}", step) for i, step in enumerate(flow.then))
    elif flow.type == "supervisor":
        refs.append(("/flow/supervisor", flow.supervisor))
        refs.extend(
            (f"/flow/workers/{i}", worker) for i, worker in enumerate(flow.workers)
        )
    else:  # graph
        refs.append(("/flow/start", flow.start))
        for i, edge in enumerate(flow.edges):
            refs.append((f"/flow/edges/{i}/from", edge.from_))
            refs.append((f"/flow/edges/{i}/to", edge.to))
    return refs


def _validate_flow_refs(project: LoadedProject, system_file: Path) -> None:
    """Mixed-flow reference resolution (docs/03 § Phase 2c): every from/to/
    step name must resolve to an agent OR a function, interchangeably."""
    known = set(project.system.agents) | set(project.system.functions)
    for pointer, name in _flow_node_refs(project.system.flow):
        if name == "END":  # graph flows may target the terminal sentinel
            continue
        if name not in known:
            raise CompileError(
                f"flow references unknown node {name!r} at {pointer}; it is "
                f"neither an agent ({', '.join(sorted(project.system.agents)) or '(none)'}) "
                f"nor a function ({', '.join(sorted(project.system.functions)) or '(none)'})",
                context={
                    "file": str(system_file),
                    "pointer": pointer,
                    "received": name,
                    "agents": sorted(project.system.agents),
                    "functions": sorted(project.system.functions),
                },
            )


def _validate_namespace(project: LoadedProject, system_file: Path) -> None:
    """Agents and functions share one node namespace (docs/21): the compiler
    resolves flow steps to either, so names cannot collide."""
    collisions = sorted(set(project.system.agents) & set(project.system.functions))
    if collisions:
        raise CompileError(
            f"node namespace collision: {', '.join(collisions)} declared as "
            "BOTH an agent and a function in system.yaml; agents and "
            "functions share one flow-node namespace",
            context={
                "file": str(system_file),
                "pointer": "/functions",
                "collisions": collisions,
            },
        )


def _resolve_flow_agent(
    project: LoadedProject, system_file: Path
) -> tuple[str, tuple[str, ...]]:
    """Phase 2 execution support: 'single' (one agent) or 'sequential'
    (exactly one agent + any number of functions). Returns (agent_name,
    execution steps)."""
    flow = project.system.flow
    if isinstance(flow, SingleFlow):
        if flow.agent not in project.agents:
            raise CompileError(
                f"flow.agent {flow.agent!r} is not in SystemSpec.agents "
                f"{sorted(project.agents)}",
                context={"file": str(system_file),
                         "pointer": "/flow/agent", "received": flow.agent},
            )
        return flow.agent, ()
    if not isinstance(flow, SequentialFlow):
        raise CompileError(
            f"Phase 2 executes only the {' / '.join(_EXECUTABLE_FLOWS)!s} "
            f"flow patterns; got {flow.type!r} (its references validated, "
            "but execution lands in Phase 3+)",
            context={"file": str(system_file),
                     "pointer": "/flow/type", "received": flow.type},
        )
    agent_steps = [step for step in flow.steps if step in project.agents]
    if len(agent_steps) != 1:
        raise CompileError(
            f"Phase 2 sequential flows need exactly ONE agent step (plus any "
            f"number of function nodes); got {len(agent_steps)} "
            f"({', '.join(agent_steps) or '(none)'}) — multi-agent flows land "
            "in Phase 3+",
            context={
                "file": str(system_file),
                "pointer": "/flow/steps",
                "agent_steps": agent_steps,
            },
        )
    return agent_steps[0], tuple(flow.steps)


# --- compilation -----------------------------------------------------------------


def compile_project(
    project_dir: Path,
    *,
    secrets: SecretsProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CompiledProject:
    """Load + validate a project; resolve provider, tools, connections,
    functions, state, retrievers, caches, memory.

    Every wiring error below is compile-time by construction: unbound slot →
    ConnectionSlotNotBoundError; accepts mismatch → CompileError; visibility
    hole → StateVisibilityError; missing version → RefResolutionError;
    namespace collision / dangling flow ref / memory-scope hole →
    CompileError; memory field misconfiguration → MemoryConfigError.
    """
    secrets = secrets or EnvSecretsProvider()
    project = load_project(project_dir)
    system_file = project.directory / "system.yaml"

    _validate_namespace(project, system_file)
    _validate_flow_refs(project, system_file)
    agent_name, flow_steps = _resolve_flow_agent(project, system_file)

    agent = project.agents[agent_name]
    agent_yaml = agent.directory / "agent.yaml"

    output_model = _load_output_schema(agent.directory, agent.spec.output.schema_ref)

    # State: compile + validate visibility (docs/22) for EVERY node — agents
    # and functions share the same structural enforcement. Each node's own
    # YAML declaration must agree with state.yaml's entry.
    node_names = [*project.system.agents, *project.system.functions]
    compiled_state = compile_state(
        project.state,
        node_names,
        where=str(project.directory / project.system.state),
    )
    for loaded_agent in project.agents.values():
        _check_node_state_visibility(
            loaded_agent.spec.name,
            loaded_agent.spec.state_visibility,
            compiled_state,
            loaded_agent.directory / "agent.yaml",
            kind="agent",
        )
    for loaded_function in project.functions.values():
        _check_node_state_visibility(
            loaded_function.spec.name,
            loaded_function.spec.state_visibility,
            compiled_state,
            loaded_function.directory / "function.yaml",
            kind="function node",
        )

    functions = {
        name: _compile_function(name, loaded)
        for name, loaded in project.functions.items()
    }

    roots = FoundryRoots.for_project(project.directory)
    prepared_connections = prepare_connections(
        project.system, roots, secrets, system_file=system_file
    )

    registry = ToolRegistry()
    tool_slots: dict[str, dict[str, Any]] = {}
    uses_tool_cache = False
    for name, binding in project.system.tools.items():
        ref = ArtifactRef.parse(binding.ref, "tool", version=binding.version)
        loaded = load_tool_version(ref, roots)
        wired = validate_tool_connection_wiring(
            name, loaded.spec, binding, prepared_connections,
            system_file=system_file,
        )
        timeout_s, retry_policy = _apply_tool_overrides(
            name, loaded.spec, binding.settings, system_file
        )
        registry.register(
            RegisteredTool(
                descriptor=ToolDescriptor(
                    name=name,
                    ref=binding.ref,
                    version=binding.version,
                    description=loaded.spec.description,
                    tags=loaded.spec.tags,
                    connection_slots=sorted(wired),
                ),
                input_schema=loaded.input_model,
                output_schema=loaded.output_model,
                handler=loaded.handler,
                timeout_s=timeout_s,
                retry_policy=retry_policy,
                auth_error_retry=any(
                    p.refresh.mode == "on_auth_error" for p in wired.values()
                ),
                cacheable=loaded.spec.cacheable,
                cache_ttl_s=loaded.spec.cache_ttl_s,
                cache_scope=loaded.spec.cache_scope,
            )
        )
        tool_slots[name] = wired
        uses_tool_cache = uses_tool_cache or loaded.spec.cacheable

    # Agent allowlists reference logical names from SystemSpec.tools.
    for loaded_agent in project.agents.values():
        unknown_tools = sorted(
            set(loaded_agent.spec.tools) - set(project.system.tools)
        )
        if unknown_tools:
            raise CompileError(
                f"agent {loaded_agent.spec.name!r} allowlists tool(s) not in "
                f"system.yaml's `tools:` block: {', '.join(unknown_tools)} "
                f"(known: {', '.join(sorted(project.system.tools)) or '(none)'})",
                context={
                    "file": str(loaded_agent.directory / "agent.yaml"),
                    "pointer": "/tools",
                    "unknown_tools": unknown_tools,
                    "known_tools": sorted(project.system.tools),
                },
            )

    # Phase 2b compile-time wiring: retriever bindings (slot wiring, config
    # validation, embedder dimension check) + semantic cache preparation.
    # Every failure below is a load-time error — nothing has been called yet.
    prepared_retrievers = prepare_retrievers(
        agent.spec.retrievers,
        roots,
        prepared_connections,
        config_file=agent_yaml,
    )
    prepared_semantic_cache = prepare_semantic_cache(
        agent.spec,
        agent.prompt_text,
        project=project.system.name,
        secrets=secrets,
        transport=transport,
    )

    # Phase 2c: memory config validation — state-field existence + type
    # (MemoryConfigError), read/write scope + retriever-slot binding
    # (CompileError), consolidator prompt on disk (MemoryConfigError).
    # An agent configuring BOTH memory and a semantic cache gets the cache
    # BYPASSED at runtime (its key covers the step's initial input, not the
    # evolving memory envelope — a hit could replay a response that ignores
    # state). Make the bypass visible at compile time (Phase 2c deviation 4).
    compile_warnings: list[CompileWarning] = []
    if agent.spec.memory is not None and prepared_semantic_cache is not None:
        compile_warnings.append(
            CompileWarning(
                agent_name=agent_name,
                category="cache.semantic.bypassed_by_memory",
                message=(
                    f"agent {agent_name!r} configures BOTH memory and "
                    "semantic_cache; the semantic cache is bypassed for "
                    "memory-enabled agents (its key covers the step's initial "
                    "input, not the evolving memory envelope). Remove "
                    "`semantic_cache:` from agent.yaml or drop `memory:` to "
                    "silence this warning."
                ),
            )
        )

    agent_view = compiled_state.agent_views[agent_name]
    prepared_memory = prepare_memory(
        agent.spec,
        agent_dir=agent.directory,
        state_field_types={
            name: field_spec.type
            for name, field_spec in project.state.state_schema.items()
        },
        read_scope=agent_view.read,
        write_scope=agent_view.write,
    )

    try:
        provider = resolve(
            agent.spec.model_binding,
            secrets,
            transport=transport,
        )
    except ProviderConfigError as exc:
        # Preserve the registry's message; append the file + field context the
        # CLI user needs (exit gate: error identifies file and field).
        raise ProviderConfigError(
            f"{exc}\n  file: {agent_yaml}\n  pointer: /model_binding/provider",
            context={
                **exc.context,
                "file": str(agent_yaml),
                "pointer": "/model_binding/provider",
            },
            cause=exc,
        ) from exc

    return CompiledProject(
        project=project,
        agent_name=agent_name,
        agent=agent,
        output_model=output_model,
        provider=provider,
        pin_set_hash=_pin_set_hash(project),
        system_version=_git_sha(project.directory),
        roots=roots,
        compiled_state=compiled_state,
        tool_registry=registry,
        tool_slots=tool_slots,
        prepared_connections=prepared_connections,
        transport=transport,
        secrets=secrets,
        retrievers=prepared_retrievers,
        semantic_cache=prepared_semantic_cache,
        uses_tool_cache=uses_tool_cache,
        functions=functions,
        flow_steps=flow_steps,
        memory=prepared_memory,
        compile_warnings=tuple(compile_warnings),
    )


def _check_node_state_visibility(
    node_name: str,
    declared: StateVisibility,
    compiled_state: CompiledState,
    config_path: Path,
    *,
    kind: str,
) -> None:
    view = compiled_state.agent_views[node_name]
    if set(declared.read) != set(view.read) or set(declared.write) != set(view.write):
        raise StateVisibilityError(
            f"{kind} {node_name!r} declares state_visibility "
            f"(read: {sorted(declared.read)}, write: {sorted(declared.write)}) "
            "that disagrees with state.yaml's visibility entry "
            f"(read: {sorted(view.read)}, write: {sorted(view.write)}); "
            "the two declarations must match",
            context={
                "file": str(config_path),
                "pointer": "/state_visibility",
                "node_declared": {"read": sorted(declared.read),
                                  "write": sorted(declared.write)},
                "state_yaml_declared": {"read": sorted(view.read),
                                        "write": sorted(view.write)},
            },
        )


def _compile_function(name: str, loaded: LoadedFunction) -> CompiledFunction:
    """Import the function handler + compute the content-hashed node_version
    (function source + config; docs/21 § What function nodes DO have)."""
    handler = _load_function_handler(loaded)
    digest = hashlib.sha256(
        (
            loaded.source_text
            + loaded.spec.model_dump_json()
        ).encode()
    ).hexdigest()[:12]
    return CompiledFunction(
        name=name,
        spec=loaded.spec,
        handler=handler,
        node_version=digest,
        directory=loaded.directory,
    )


def _load_function_handler(loaded: LoadedFunction) -> FunctionHandler:
    """Import 'function.py::callable_name' relative to the function dir and
    enforce the docs/12 signature: async def <name>(state_view, ctx)."""
    ref = loaded.spec.function
    where = str(loaded.directory / "function.yaml")
    if "::" not in ref:
        raise CompileError(
            f"function ref must look like 'function.py::callable_name'; "
            f"got {ref!r}",
            context={"file": where, "pointer": "/function", "received": ref},
        )
    file_part, callable_name = ref.split("::", 1)
    module_path = loaded.directory / file_part
    digest = hashlib.sha256(str(module_path).encode()).hexdigest()[:12]
    module_name = f"_foundry_function_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise CompileError(
            f"could not import function module: {module_path}",
            context={"file": where, "ref": ref},
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    handler = getattr(module, callable_name, None)
    if handler is None:
        raise CompileError(
            f"{callable_name!r} not found in {module_path}",
            context={"file": where, "ref": ref},
        )
    import inspect

    if not inspect.iscoroutinefunction(handler):
        raise CompileError(
            f"function node handler {ref!r} at {module_path} must be an "
            "async function (`async def <name>(state_view, ctx)`)",
            context={"file": where, "ref": ref},
        )
    params = list(inspect.signature(handler).parameters)
    if tuple(params[:2]) != ("state_view", "ctx") or len(params) != 2:
        raise CompileError(
            f"function node handler {ref!r} has signature "
            f"({', '.join(params)}); expected exactly (state_view, ctx) — "
            "the compiler introspects by name (docs/12 § FunctionNodeSpec)",
            context={"file": where, "ref": ref, "received_params": params},
        )
    return cast(FunctionHandler, handler)


def _apply_tool_overrides(
    name: str,
    spec: ToolSpec,
    settings: dict[str, Any],
    system_file: Path,
) -> tuple[float, RetryPolicy]:
    timeout_s = float(spec.timeout_s)
    retry_policy = spec.retry_policy
    for key, value in settings.items():
        if key not in _OVERRIDABLE_SETTINGS or key not in spec.overridable_settings:
            raise CompileError(
                f"tool {name!r} does not allow overriding setting {key!r} "
                f"(overridable: {', '.join(spec.overridable_settings)})",
                context={
                    "file": str(system_file),
                    "pointer": f"/tools/{name}/settings/{key}",
                    "overridable": spec.overridable_settings,
                },
            )
        if key == "timeout_s":
            timeout_s = float(value)
        elif key == "retry_policy":
            try:
                retry_policy = RetryPolicy.model_validate(value)
            except ValidationError as exc:
                raise CompileError(
                    f"tool {name!r} retry_policy override is invalid: "
                    f"{exc.errors()[0]['msg']}",
                    context={
                        "file": str(system_file),
                        "pointer": f"/tools/{name}/settings/retry_policy",
                    },
                    cause=exc,
                ) from exc
    return timeout_s, retry_policy


def _load_output_schema(agent_dir: Path, ref: str) -> type[BaseModel]:
    """Import 'module.py::ClassName' relative to the agent directory."""
    if "::" not in ref:
        raise CompileError(
            f"output schema ref must look like 'output_schema.py::ClassName'; "
            f"got {ref!r}",
            context={"agent_dir": str(agent_dir), "ref": ref},
        )
    file_part, class_name = ref.split("::", 1)
    module_path = agent_dir / file_part
    if not module_path.exists():
        raise CompileError(
            f"output schema module not found: {module_path}",
            context={"agent_dir": str(agent_dir), "ref": ref},
        )
    digest = hashlib.sha256(str(module_path).encode()).hexdigest()[:12]
    module_name = f"_foundry_output_schema_{digest}"
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise CompileError(
            f"could not import output schema module: {module_path}",
            context={"ref": ref},
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    cls = getattr(module, class_name, None)
    if cls is None or not (isinstance(cls, type) and issubclass(cls, BaseModel)):
        raise CompileError(
            f"{class_name!r} in {module_path} is missing or not a Pydantic "
            "BaseModel subclass",
            context={"ref": ref, "module": str(module_path)},
        )
    return cls


def _pin_set_hash(project: LoadedProject) -> str:
    payload = json.dumps(
        {
            "system": project.system.model_dump(mode="json", by_alias=True),
            "agents": {
                name: a.spec.model_dump(mode="json", by_alias=True)
                for name, a in sorted(project.agents.items())
            },
            "functions": {
                name: f.spec.model_dump(mode="json", by_alias=True)
                for name, f in sorted(project.functions.items())
            },
        },
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def _git_sha(directory: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=directory,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unversioned"
    sha = result.stdout.strip()
    return sha if result.returncode == 0 and sha else "unversioned"


# --- execution --------------------------------------------------------------------


async def run_project(
    compiled: CompiledProject,
    input_data: dict[str, Any],
    session: Session,
    event_sink: EventSink | None = None,
    *,
    pool: InProcessConnectionPool | None = None,
) -> RunResult:
    """Run the compiled system through a LangGraph StateGraph.

    Single flow: one agent node. Sequential flow: one node per step (function
    nodes + the agent), chained; the project state dict threads through every
    node with reducer-merged deltas and per-node visibility projections.
    """
    emitter = EventEmitter(session, event_sink)
    started = datetime.now(UTC)
    counters = RunCounters()
    pool = pool or InProcessConnectionPool()
    retriever_accessor: MappingRetrieverAccessor | None = None
    retriever_conn_accessors: list[Any] = []
    reducers = compiled.compiled_state.reducers

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

    async def agent_node(state: GraphState) -> dict[str, Any]:
        run_state = state.get("state", {})
        delta, output = await run_agent_step(
            compiled, run_state, session, emitter, pool,
            retriever_accessor, counters,
        )
        return {
            "state": apply_delta(run_state, delta, reducers),
            "output": output,
        }

    def make_function_node(function: CompiledFunction) -> Any:
        async def function_node(state: GraphState) -> dict[str, Any]:
            run_state = state.get("state", {})
            delta = await run_function_step(
                compiled, function, run_state, session, emitter
            )
            return {"state": apply_delta(run_state, delta, reducers)}

        return function_node

    graph = StateGraph(GraphState)
    if compiled.flow_steps:
        previous: Any = START
        for step in compiled.flow_steps:
            if step == compiled.agent_name:
                graph.add_node(step, agent_node)
            else:
                graph.add_node(step, make_function_node(compiled.functions[step]))
            graph.add_edge(previous, step)
            previous = step
        graph.add_edge(previous, END)
    else:
        graph.add_node("agent", agent_node)
        graph.add_edge(START, "agent")
        graph.add_edge("agent", END)
    app = graph.compile()

    inputs_hash = hashlib.sha256(
        json.dumps(input_data, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]
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
        final = await app.ainvoke({"state": seed_state(compiled, input_data)})
    except FoundryError as exc:
        emitter.emit(RunFailed, error=exc.to_dict())
        await _release_retrievers(retriever_conn_accessors)
        await pool.close_all()
        raise
    except Exception as exc:  # wrap: no arbitrary exceptions cross the boundary
        wrapped = OrchestrationError(
            f"run failed with an unclassified error: {exc}",
            context={"project": compiled.project.system.name},
            cause=exc,
        )
        emitter.emit(RunFailed, error=wrapped.to_dict())
        await _release_retrievers(retriever_conn_accessors)
        await pool.close_all()
        raise wrapped from exc

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
    metrics = pool.metrics_snapshot()
    await _release_retrievers(retriever_conn_accessors)
    await pool.close_all()
    return RunResult(
        output=output,
        response=last_response,
        pool_metrics=metrics,
        llm_call_count=counters.llm_call_count,
        final_state=final_state,
    )


async def _release_retrievers(accessors: list[Any]) -> None:
    for accessor in accessors:
        await accessor.release_all()


__all__ = [
    "CompiledFunction",
    "CompiledProject",
    "RunResult",
    "compile_project",
    "run_project",
]
