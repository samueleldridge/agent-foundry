"""LangGraph runtime adapter — compile + run a single-agent graph.

The ONLY module (with ``_langgraph_types``) allowed to import ``langgraph`` /
``langchain_core`` (import-boundary lint). Phase 2a scope: compile a
``SystemSpec`` with a ``single`` flow into a one-node ``StateGraph`` whose
node runs the agent's LLM ⇄ tool loop — model → tool call (pooled,
authenticated connection) → tool result → model → final output.
Checkpointers, streaming, and multi-node flows land in Phase 3; the real
compiler (``foundry.orchestration.compiler``) also lands there — this module
deliberately stays a thin adapter.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from foundry.cache import (
    InProcessResultCache,
    PreparedSemanticCache,
    default_result_cache_path,
    ensure_version_marker,
    prepare_semantic_cache,
    semantic_lookup,
    semantic_store,
)
from foundry.catalog.loader import load_tool_version
from foundry.config import (
    ArtifactRef,
    EnvSecretsProvider,
    FoundryRoots,
    LoadedAgent,
    LoadedProject,
    ToolSpec,
    load_project,
)
from foundry.config.secrets import SecretsProvider
from foundry.connections import (
    InProcessConnectionPool,
    PreparedConnection,
    SlotConnectionAccessor,
    prepare_connections,
    validate_tool_connection_wiring,
)
from foundry.core import (
    AgentCompleted,
    AgentStarted,
    CacheBundle,
    ConnectionContext,
    FoundryMessage,
    LLMCallCompleted,
    LLMCallStarted,
    MessageRole,
    ModelResponse,
    RegisteredTool,
    ResultCache,
    RetryPolicy,
    RunCompleted,
    RunFailed,
    RunStarted,
    Session,
    TextBlock,
    ToolDescriptor,
    ToolRegistry,
    ToolResultBlock,
    ToolUseBlock,
)
from foundry.core.errors import (
    CompileError,
    FoundryError,
    IterationLimitError,
    OrchestrationError,
    ProviderConfigError,
    StateVisibilityError,
    ToolError,
)
from foundry.core.errors import (
    ConnectionError as FoundryConnectionError,
)
from foundry.core.tool import RunContext
from foundry.orchestration.state_scope import (
    AgentStateView,
    CompiledState,
    compile_state,
)
from foundry.providers import ProviderAdapter, ToolSchema, resolve
from foundry.retrieval import (
    MappingRetrieverAccessor,
    PreparedRetriever,
    build_retriever_accessor,
    prepare_retrievers,
)
from foundry.runtime._langgraph_types import GraphState

EventSink = Callable[[BaseModel], None]

_OVERRIDABLE_SETTINGS = ("timeout_s", "retry_policy")


# --- compilation -----------------------------------------------------------------


@dataclass(frozen=True)
class CompiledProject:
    """A Phase 2a compiled system: one agent, its tools + connections + state."""

    project: LoadedProject
    agent_name: str
    agent: LoadedAgent
    output_model: type[BaseModel]
    provider: ProviderAdapter
    pin_set_hash: str
    system_version: str
    roots: FoundryRoots
    compiled_state: CompiledState
    tool_registry: ToolRegistry = field(default_factory=ToolRegistry)
    tool_slots: dict[str, dict[str, PreparedConnection]] = field(default_factory=dict)
    prepared_connections: dict[str, PreparedConnection] = field(default_factory=dict)
    transport: httpx.AsyncBaseTransport | None = None
    secrets: SecretsProvider = field(default_factory=EnvSecretsProvider)
    retrievers: dict[str, PreparedRetriever] = field(default_factory=dict)
    """Prepared retriever bindings for the single agent (Phase 2b)."""
    semantic_cache: PreparedSemanticCache | None = None
    uses_tool_cache: bool = False
    """True when any registered tool opted into result caching."""

    @property
    def pins(self) -> dict[str, Any]:
        """Pinned tool + connection versions — recorded in run metadata."""
        return {
            "tools": {
                name: f"{binding.ref}@{binding.version}"
                for name, binding in self.project.system.tools.items()
            },
            "connections": {
                name: f"{binding.ref}@{binding.version}"
                for name, binding in self.project.system.connections.items()
            },
        }


def compile_project(
    project_dir: Path,
    *,
    secrets: SecretsProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CompiledProject:
    """Load + validate a project; resolve provider, tools, connections, state.

    Every wiring error below is compile-time by construction: unbound slot →
    ConnectionSlotNotBoundError; accepts mismatch → CompileError; visibility
    hole → StateVisibilityError; missing version → RefResolutionError.
    """
    secrets = secrets or EnvSecretsProvider()
    project = load_project(project_dir)
    system_file = project.directory / "system.yaml"
    flow = project.system.flow
    if flow.type != "single":
        raise CompileError(
            f"Phase 2a supports only the 'single' flow pattern; "
            f"got {flow.type!r} (multi-node flows land in Phase 3+)",
            context={"file": str(system_file),
                     "pointer": "/flow/type", "received": flow.type},
        )
    agent_name = flow.agent
    if agent_name not in project.agents:
        raise CompileError(
            f"flow.agent {agent_name!r} is not in SystemSpec.agents "
            f"{sorted(project.agents)}",
            context={"file": str(system_file),
                     "pointer": "/flow/agent", "received": agent_name},
        )
    agent = project.agents[agent_name]
    agent_yaml = agent.directory / "agent.yaml"

    output_model = _load_output_schema(agent.directory, agent.spec.output.schema_ref)

    # State: compile + validate visibility (docs/22). agent.yaml's
    # state_visibility must agree with state.yaml's entry for the agent.
    compiled_state = compile_state(
        project.state,
        project.system.agents,
        where=str(project.directory / project.system.state),
    )
    _check_agent_state_visibility(agent, compiled_state, agent_yaml)

    roots = FoundryRoots.for_project(project.directory)
    prepared_connections = prepare_connections(
        project.system, roots, secrets, system_file=system_file
    )

    registry = ToolRegistry()
    tool_slots: dict[str, dict[str, PreparedConnection]] = {}
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
    )


def _check_agent_state_visibility(
    agent: LoadedAgent, compiled_state: CompiledState, agent_yaml: Path
) -> None:
    view = compiled_state.agent_views[agent.spec.name]
    declared = agent.spec.state_visibility
    if set(declared.read) != set(view.read) or set(declared.write) != set(view.write):
        raise StateVisibilityError(
            f"agent {agent.spec.name!r} declares state_visibility "
            f"(read: {sorted(declared.read)}, write: {sorted(declared.write)}) "
            "that disagrees with state.yaml's visibility entry "
            f"(read: {sorted(view.read)}, write: {sorted(view.write)}); "
            "the two declarations must match",
            context={
                "file": str(agent_yaml),
                "pointer": "/state_visibility",
                "agent_declared": {"read": sorted(declared.read),
                                   "write": sorted(declared.write)},
                "state_yaml_declared": {"read": sorted(view.read),
                                        "write": sorted(view.write)},
            },
        )


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


@dataclass(frozen=True)
class RunResult:
    output: Any
    response: ModelResponse | None
    pool_metrics: dict[str, Any] = field(default_factory=dict)
    llm_call_count: int = 0
    """Actual provider calls made — 0 on a semantic-cache hit."""


class _EventEmitter:
    """Sequence-stamped event emission (event-stream invariant 1)."""

    def __init__(self, session: Session, sink: EventSink | None) -> None:
        self._session = session
        self._sink = sink
        self._sequence = 0

    def emit(self, event_cls: type[BaseModel], **fields: Any) -> None:
        event = event_cls(
            run_id=self._session.run_id,
            sequence=self._sequence,
            timestamp=datetime.now(UTC),
            **fields,
        )
        self._sequence += 1
        if self._sink is not None:
            self._sink(event)
        if self._session.logger is not None:
            self._session.logger.info(
                str(getattr(event, "event", event_cls.__name__)),
                sequence=event.sequence,  # type: ignore[attr-defined]
            )


def _build_messages(
    compiled: CompiledProject,
    agent_input: dict[str, Any],
    tool_descriptions: str,
) -> list[FoundryMessage]:
    schema_json = json.dumps(compiled.output_model.model_json_schema(), indent=2)
    system_text = (
        compiled.agent.prompt_text.rstrip()
        + (
            "\n\nYou can call the following tools when they help:\n"
            + tool_descriptions
            if tool_descriptions
            else ""
        )
        + "\n\nWhen you give your final answer, respond ONLY with a single "
        "JSON object that validates against this JSON Schema — no code "
        "fences, no commentary:\n"
        + schema_json
    )
    return [
        FoundryMessage(role=MessageRole.SYSTEM, content=[TextBlock(text=system_text)]),
        FoundryMessage(
            role=MessageRole.USER,
            content=[TextBlock(text=json.dumps(agent_input))],
        ),
    ]


def _parse_output(compiled: CompiledProject, response: ModelResponse) -> BaseModel:
    text = "".join(
        b.text for b in response.message.content if isinstance(b, TextBlock)
    ).strip()
    if text.startswith("```"):
        text = text.strip("`\n")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        return compiled.output_model.model_validate_json(text)
    except ValidationError as exc:
        raise OrchestrationError(
            f"agent {compiled.agent_name!r} output failed validation against "
            f"output schema {compiled.output_model.__name__!r}",
            context={
                "agent": compiled.agent_name,
                "output_schema": compiled.output_model.__name__,
                "response_preview": text[:500],
            },
            cause=exc,
        ) from exc


def _tool_schemas(compiled: CompiledProject) -> list[ToolSchema]:
    allow = set(compiled.agent.spec.tools)
    schemas: list[ToolSchema] = []
    for descriptor in compiled.tool_registry.list_all():
        if descriptor.name not in allow:
            continue
        registered = compiled.tool_registry.get(descriptor.name)
        assert registered is not None  # descriptor came from the registry
        schemas.append(
            ToolSchema(
                name=descriptor.name,
                description=descriptor.description,
                input_schema=registered.input_schema.model_json_schema(),
            )
        )
    return schemas


def _tool_descriptions(schemas: list[ToolSchema]) -> str:
    return "\n".join(f"- {s.name}: {s.description}" for s in schemas)


async def _dispatch_one(
    compiled: CompiledProject,
    pool: InProcessConnectionPool,
    session: Session,
    emitter: _EventEmitter,
    block: ToolUseBlock,
    retrievers: MappingRetrieverAccessor | None = None,
) -> ToolResultBlock:
    """One tool call → one tool_result block. Tool-layer errors become
    structured is_error results the LLM can recover from (docs/20 § Error
    semantics); non-tool errors (cost budget, cancellation) propagate."""
    registered = compiled.tool_registry.get(block.name)
    accessor = SlotConnectionAccessor(
        pool,
        compiled.project.system.name,
        compiled.tool_slots.get(block.name, {}),
        ConnectionContext(http_transport=compiled.transport),
        agent_name=compiled.agent_name,
        emit=emitter.emit,
    )
    ctx = RunContext(
        run_id=str(session.run_id),
        agent_name=compiled.agent_name,
        session=session,
        tool_ref=(
            f"{registered.descriptor.ref}@{registered.descriptor.version}"
            if registered is not None
            else block.name
        ),
        timeout_s=registered.timeout_s if registered is not None else None,
        retry_policy=(
            registered.retry_policy if registered is not None else RetryPolicy()
        ),
        connections=accessor,
        retrievers=retrievers,
    )
    try:
        output = await compiled.tool_registry.dispatch(
            block.name,
            compiled.agent.spec.tools,
            block.input,
            ctx,
            emit=emitter.emit,
        )
    except (ToolError, FoundryConnectionError) as exc:
        return ToolResultBlock(
            tool_use_id=block.id,
            is_error=True,
            content=[TextBlock(text=f"{type(exc).__name__}: {exc}")],
        )
    finally:
        await accessor.release_all()
    return ToolResultBlock(
        tool_use_id=block.id,
        content=[TextBlock(text=output.model_dump_json())],
    )


async def run_project(
    compiled: CompiledProject,
    input_data: dict[str, Any],
    session: Session,
    event_sink: EventSink | None = None,
    *,
    pool: InProcessConnectionPool | None = None,
) -> RunResult:
    """Run the compiled single-agent system through a LangGraph StateGraph.

    The agent node runs the LLM ⇄ tool loop: tool_use blocks dispatch through
    the ToolRegistry (with pooled connections), results feed back as
    tool_result blocks, until a terminal response or iteration_limit.
    """
    emitter = _EventEmitter(session, event_sink)
    started = datetime.now(UTC)
    last_response: ModelResponse | None = None
    llm_call_count = 0
    pool = pool or InProcessConnectionPool()
    view: AgentStateView = compiled.compiled_state.agent_views[compiled.agent_name]
    retriever_accessor: MappingRetrieverAccessor | None = None
    retriever_conn_accessors: list[SlotConnectionAccessor] = []

    # Session-scoped cache bundle (docs/10 § CacheAccessor on Session): the
    # dispatcher reads tool_result; the semantic side is driven below.
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
        nonlocal last_response, llm_call_count
        spec = compiled.agent.spec
        emitter.emit(
            AgentStarted,
            agent_name=compiled.agent_name,
            agent_version=spec.prompt.version,
        )
        # Structural visibility: the agent sees ONLY its `read` fields —
        # everything else is absent from its projection, not None-ed.
        agent_input = view.project_input(state.get("input", {}))
        schemas = _tool_schemas(compiled)
        messages = _build_messages(
            compiled, agent_input, _tool_descriptions(schemas)
        )

        # Semantic cache (docs/24 § Layer 2): keyed by the agent's INITIAL
        # input; a hit short-circuits the whole LLM ⇄ tool loop and replays
        # the cached terminal response. Every failure inside fails open.
        semantic = compiled.semantic_cache
        cache_key = None
        response: ModelResponse | None = None
        if semantic is not None:
            await ensure_version_marker(
                semantic, compiled.agent_name, emitter.emit
            )
            response, cache_key = await semantic_lookup(
                semantic,
                agent_name=compiled.agent_name,
                model_binding=spec.model_binding,
                tools=schemas,
                messages=messages,
                emit=emitter.emit,
            )

        cache_hit = response is not None
        if not cache_hit:
            for _round in range(spec.iteration_limit):
                emitter.emit(
                    LLMCallStarted,
                    agent_name=compiled.agent_name,
                    provider=compiled.provider.name,
                    model=compiled.provider.model,
                )
                response = await compiled.provider.generate(
                    messages,
                    schemas,
                    spec.model_binding.settings,
                    session,
                )
                llm_call_count += 1
                last_response = response
                emitter.emit(
                    LLMCallCompleted,
                    agent_name=compiled.agent_name,
                    usage=response.usage,
                    cost_estimate_usd=response.cost_estimate_usd,
                    latency_ms=response.latency_ms,
                    stop_reason=response.stop_reason,
                )
                tool_uses = [
                    b for b in response.message.content
                    if isinstance(b, ToolUseBlock)
                ]
                if not tool_uses:
                    break
                # Parallel tool calls within one round (docs/21).
                results = list(
                    await asyncio.gather(
                        *(
                            _dispatch_one(
                                compiled, pool, session, emitter, block,
                                retriever_accessor,
                            )
                            for block in tool_uses
                        )
                    )
                )
                messages.append(response.message)
                messages.append(
                    FoundryMessage(role=MessageRole.USER, content=list(results))
                )
            else:
                raise IterationLimitError(
                    f"agent {compiled.agent_name!r} exceeded its "
                    f"iteration_limit of {spec.iteration_limit} LLM rounds "
                    "without a terminal response",
                    context={"agent": compiled.agent_name,
                             "iteration_limit": spec.iteration_limit},
                )

        # NOTE: last_response tracks ACTUAL provider calls only — on a cache
        # hit it stays None so run totals report zero spend (the saving is on
        # the cache.semantic.hit event instead).
        assert response is not None  # cache hit or the loop ran at least once
        if semantic is not None and cache_key is not None and not cache_hit:
            # Store the TERMINAL response keyed by the initial input — a
            # future hit replays the final answer without the tool loop.
            await semantic_store(
                semantic, cache_key, response,
                agent_name=compiled.agent_name, emit=emitter.emit,
            )
        output = _parse_output(compiled, response)
        emitter.emit(
            AgentCompleted,
            agent_name=compiled.agent_name,
            output_summary=f"{compiled.output_model.__name__} produced",
        )
        return {"output": output.model_dump(mode="json")}

    graph = StateGraph(GraphState)
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
        final_state = await app.ainvoke({"input": input_data})
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

    duration_ms = int((datetime.now(UTC) - started).total_seconds() * 1000)
    usage = last_response.usage if last_response else None
    emitter.emit(
        RunCompleted,
        status="success",
        final_output=final_state.get("output"),
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
        output=final_state.get("output"),
        response=last_response,
        pool_metrics=metrics,
        llm_call_count=llm_call_count,
    )


async def _release_retrievers(accessors: list[SlotConnectionAccessor]) -> None:
    for accessor in accessors:
        await accessor.release_all()


__all__ = [
    "CompiledProject",
    "RunResult",
    "compile_project",
    "run_project",
]
