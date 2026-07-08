"""Step execution: the agent step (with optional memory) + function-node
step + shared event emission. No langgraph imports — graph wiring lives in
``langgraph_adapter``; this module is plain asyncio so the Phase 3 compiler
can reuse it per node.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ValidationError

from foundry.cache import ensure_version_marker, semantic_lookup, semantic_store
from foundry.config import WorkingMemoryLayerConfig
from foundry.connections import InProcessConnectionPool, SlotConnectionAccessor
from foundry.core import (
    AgentCompleted,
    AgentStarted,
    ConnectionContext,
    FoundryMessage,
    FunctionNodeCompleted,
    FunctionNodeStarted,
    LLMCallCompleted,
    LLMCallStarted,
    MemoryContext,
    MemoryWrite,
    MessageRole,
    ModelResponse,
    Reducer,
    RetryPolicy,
    Session,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    WarningEvent,
    apply_reducer,
)
from foundry.core.errors import (
    ConnectionError as FoundryConnectionError,
)
from foundry.core.errors import (
    FoundryError,
    IterationLimitError,
    OrchestrationError,
    ToolError,
)
from foundry.core.tool import RunContext
from foundry.memory import DefaultMemory, build_memory, weave
from foundry.orchestration.state_scope import AgentStateView
from foundry.providers import ToolSchema
from foundry.retrieval import MappingRetrieverAccessor
from foundry.runtime.compiled import CompiledFunction, CompiledProject

EventSink = Callable[[BaseModel], None]

_TURNS_FIELD = "turns"
"""Phase 2c multi-turn convention: a memory-enabled agent whose read scope
projects a list-of-strings field named ``turns`` converses once per item.
The real conversation surface (checkpointed sessions / API) is Phase 3+."""


class EventEmitter:
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


class RunCounters:
    """Mutable per-run tallies shared across steps."""

    def __init__(self) -> None:
        self.llm_call_count = 0
        self.last_response: ModelResponse | None = None


# --- state helpers ---------------------------------------------------------------


def seed_state(
    compiled: CompiledProject, input_data: dict[str, Any]
) -> dict[str, Any]:
    """Initial run state: explicit schema defaults, overlaid with the input
    keys that name state fields. Non-state input keys are dropped — nodes
    only ever see projections of declared fields."""
    schema = compiled.project.state.state_schema
    state: dict[str, Any] = {
        name: spec.default
        for name, spec in schema.items()
        if spec.default is not None
    }
    state.update({k: v for k, v in input_data.items() if k in schema})
    return state


def apply_delta(
    state: dict[str, Any],
    delta: dict[str, Any],
    reducers: dict[str, Reducer],
) -> dict[str, Any]:
    new_state = dict(state)
    for field_name, value in delta.items():
        new_state[field_name] = apply_reducer(
            reducers.get(field_name, Reducer.LAST_WRITE_WINS),
            state.get(field_name),
            value,
        )
    return new_state


# --- prompt helpers ---------------------------------------------------------------


def _system_text(compiled: CompiledProject, tool_descriptions: str) -> str:
    schema_json = json.dumps(compiled.output_model.model_json_schema(), indent=2)
    return (
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


def build_messages(
    compiled: CompiledProject,
    agent_input: dict[str, Any],
    tool_descriptions: str,
) -> list[FoundryMessage]:
    return [
        FoundryMessage(
            role=MessageRole.SYSTEM,
            content=[TextBlock(text=_system_text(compiled, tool_descriptions))],
        ),
        FoundryMessage(
            role=MessageRole.USER,
            content=[TextBlock(text=json.dumps(agent_input))],
        ),
    ]


def parse_output(compiled: CompiledProject, response: ModelResponse) -> BaseModel:
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


def tool_schemas(compiled: CompiledProject) -> list[ToolSchema]:
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


def tool_descriptions(schemas: list[ToolSchema]) -> str:
    return "\n".join(f"- {s.name}: {s.description}" for s in schemas)


# --- tool dispatch ------------------------------------------------------------------


async def _dispatch_one(
    compiled: CompiledProject,
    pool: InProcessConnectionPool,
    session: Session,
    emitter: EventEmitter,
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


async def llm_tool_loop(
    compiled: CompiledProject,
    messages: list[FoundryMessage],
    schemas: list[ToolSchema],
    session: Session,
    emitter: EventEmitter,
    pool: InProcessConnectionPool,
    retrievers: MappingRetrieverAccessor | None,
    counters: RunCounters,
) -> ModelResponse:
    """The LLM ⇄ tool loop: generate → dispatch tool_use blocks in parallel →
    feed results back → repeat until a terminal response or iteration_limit.
    Mutates ``messages`` in place (the conversation grows)."""
    spec = compiled.agent.spec
    capture = compiled.project.system.observability.capture_inputs
    for _round in range(spec.iteration_limit):
        emitter.emit(
            LLMCallStarted,
            agent_name=compiled.agent_name,
            provider=compiled.provider.name,
            model=compiled.provider.model,
            prompt_messages=list(messages) if capture else None,
        )
        response = await compiled.provider.generate(
            messages,
            schemas,
            spec.model_binding.settings,
            session,
        )
        counters.llm_call_count += 1
        counters.last_response = response
        emitter.emit(
            LLMCallCompleted,
            agent_name=compiled.agent_name,
            usage=response.usage,
            cost_estimate_usd=response.cost_estimate_usd,
            latency_ms=response.latency_ms,
            stop_reason=response.stop_reason,
        )
        tool_uses = [
            b for b in response.message.content if isinstance(b, ToolUseBlock)
        ]
        if not tool_uses:
            return response
        # Parallel tool calls within one round (docs/21).
        results = list(
            await asyncio.gather(
                *(
                    _dispatch_one(compiled, pool, session, emitter, block, retrievers)
                    for block in tool_uses
                )
            )
        )
        messages.append(response.message)
        messages.append(FoundryMessage(role=MessageRole.USER, content=list(results)))
    raise IterationLimitError(
        f"agent {compiled.agent_name!r} exceeded its iteration_limit of "
        f"{spec.iteration_limit} LLM rounds without a terminal response",
        context={"agent": compiled.agent_name,
                 "iteration_limit": spec.iteration_limit},
    )


# --- agent step ---------------------------------------------------------------------


def _output_delta(
    output: BaseModel, view: AgentStateView
) -> dict[str, Any]:
    """Project the agent's output model onto its write scope. Out-of-scope
    output fields are normal for agents (the output schema is the caller's
    contract; state gets the declared projection) — no warning, unlike
    function nodes whose return value IS a state delta."""
    dump = output.model_dump(mode="json")
    return {k: v for k, v in dump.items() if k in view.write}


async def run_agent_step(
    compiled: CompiledProject,
    run_state: dict[str, Any],
    session: Session,
    emitter: EventEmitter,
    pool: InProcessConnectionPool,
    retrievers: MappingRetrieverAccessor | None,
    counters: RunCounters,
) -> tuple[dict[str, Any], Any]:
    """One agent step → (state delta, final output). Dispatches to the
    memory-enabled turn loop when the agent configures memory."""
    spec = compiled.agent.spec
    emitter.emit(
        AgentStarted,
        agent_name=compiled.agent_name,
        agent_version=spec.prompt.version,
    )
    view = compiled.compiled_state.agent_views[compiled.agent_name]
    schemas = tool_schemas(compiled)

    if compiled.memory is not None:
        delta, output_dump = await _memory_agent_turns(
            compiled, run_state, session, emitter, pool, retrievers,
            counters, view, schemas,
        )
        emitter.emit(
            AgentCompleted,
            agent_name=compiled.agent_name,
            output_summary=f"{compiled.output_model.__name__} produced",
        )
        return delta, output_dump

    # --- memory-off path (Phase 2a/2b behaviour, semantic cache included) ---
    agent_input = view.project_input(run_state)
    messages = build_messages(compiled, agent_input, tool_descriptions(schemas))

    # Semantic cache (docs/24 § Layer 2): keyed by the agent's INITIAL
    # input; a hit short-circuits the whole LLM ⇄ tool loop and replays
    # the cached terminal response. Every failure inside fails open.
    semantic = compiled.semantic_cache
    cache_key = None
    response: ModelResponse | None = None
    if semantic is not None:
        await ensure_version_marker(semantic, compiled.agent_name, emitter.emit)
        response, cache_key = await semantic_lookup(
            semantic,
            agent_name=compiled.agent_name,
            model_binding=spec.model_binding,
            tools=schemas,
            messages=messages,
            emit=emitter.emit,
        )

    cache_hit = response is not None
    if response is None:
        response = await llm_tool_loop(
            compiled, messages, schemas, session, emitter, pool,
            retrievers, counters,
        )

    if semantic is not None and cache_key is not None and not cache_hit:
        # Store the TERMINAL response keyed by the initial input — a
        # future hit replays the final answer without the tool loop.
        await semantic_store(
            semantic, cache_key, response,
            agent_name=compiled.agent_name, emit=emitter.emit,
        )
    output = parse_output(compiled, response)
    emitter.emit(
        AgentCompleted,
        agent_name=compiled.agent_name,
        output_summary=f"{compiled.output_model.__name__} produced",
    )
    return _output_delta(output, view), output.model_dump(mode="json")


def _extract_turns(agent_input: dict[str, Any]) -> list[str]:
    raw = agent_input.get(_TURNS_FIELD)
    if isinstance(raw, list) and raw and all(isinstance(t, str) for t in raw):
        return list(raw)
    return [json.dumps(agent_input)]


def _response_text(response: ModelResponse) -> str:
    return "".join(
        b.text for b in response.message.content if isinstance(b, TextBlock)
    ).strip()


async def _memory_agent_turns(
    compiled: CompiledProject,
    run_state: dict[str, Any],
    session: Session,
    emitter: EventEmitter,
    pool: InProcessConnectionPool,
    retrievers: MappingRetrieverAccessor | None,
    counters: RunCounters,
    view: AgentStateView,
    schemas: list[ToolSchema],
) -> tuple[dict[str, Any], Any]:
    """The docs/26 per-turn lifecycle: memory.read → weave → LLM ⇄ tools →
    state append + memory.write → periodic consolidate. Runs once per item
    of the ``turns`` read-scope field (single turn when absent).

    NOTE: the semantic cache is bypassed for memory-enabled agents in Phase
    2c — its key covers the step's initial input, not the evolving memory
    envelope, so a hit could replay a response that ignores state."""
    assert compiled.memory is not None
    prepared = compiled.memory
    memory: DefaultMemory = build_memory(
        prepared,
        agent_name=compiled.agent_name,
        provider=compiled.provider,
        retrievers=retrievers,
        emit=emitter.emit,
    )
    memory_config = prepared.spec.memory
    assert memory_config is not None
    reducers = compiled.compiled_state.reducers
    schema_types = {
        name: field_spec.type.replace(" ", "")
        for name, field_spec in compiled.project.state.state_schema.items()
    }
    message_fields = [
        layer.source_field
        for layer in memory_config.layers
        if isinstance(layer, WorkingMemoryLayerConfig)
        and layer.source_field in view.write
        and schema_types.get(layer.source_field) == "list[FoundryMessage]"
    ]

    local_state = dict(run_state)
    delta: dict[str, Any] = {}
    pending_appends: dict[str, list[FoundryMessage]] = {}

    def write_field(field_name: str, value: Any) -> None:
        if field_name not in view.write:
            emitter.emit(
                WarningEvent,
                agent_name=compiled.agent_name,
                category="memory.out_of_scope_write",
                message=f"memory write to state field {field_name!r} dropped: "
                f"outside agent {compiled.agent_name!r}'s write scope",
                error_class=None,
            )
            return
        local_state[field_name] = value
        delta[field_name] = value

    def append_messages(new_messages: list[FoundryMessage]) -> None:
        for field_name in message_fields:
            current = list(local_state.get(field_name) or [])
            local_state[field_name] = [*current, *new_messages]
            if reducers.get(field_name) is Reducer.APPEND:
                pending = pending_appends.setdefault(field_name, [])
                pending.extend(new_messages)
                delta[field_name] = list(pending)
            else:
                delta[field_name] = local_state[field_name]

    def make_ctx(turn_count: int, recent: list[FoundryMessage]) -> MemoryContext:
        return MemoryContext(
            run_id=session.run_id,
            agent_name=compiled.agent_name,
            session=session,
            state_view=view.project_input(local_state),
            state_writer=write_field,
            turn_count=turn_count,
            recent_messages=list(recent),
        )

    turns = _extract_turns(view.project_input(local_state))
    descriptions = tool_descriptions(schemas)
    recent: list[FoundryMessage] = []
    turn_count = 0
    output: BaseModel | None = None

    for turn_text in turns:
        ctx = make_ctx(turn_count, recent)
        envelope = await memory.read(turn_text, ctx)
        woven = weave(_system_text(compiled, descriptions), envelope, memory_config)
        user_text = (
            f"{woven.user_prefix}\n\n{turn_text}" if woven.user_prefix else turn_text
        )
        messages = [
            FoundryMessage(
                role=MessageRole.SYSTEM,
                content=[TextBlock(text=woven.system_text)],
            ),
            *woven.memory_messages,
            FoundryMessage(role=MessageRole.USER, content=[TextBlock(text=user_text)]),
        ]
        response = await llm_tool_loop(
            compiled, messages, schemas, session, emitter, pool,
            retrievers, counters,
        )
        output = parse_output(compiled, response)
        assistant_text = _response_text(response)
        turn_messages = [
            FoundryMessage(
                role=MessageRole.USER, content=[TextBlock(text=turn_text)]
            ),
            FoundryMessage(
                role=MessageRole.ASSISTANT,
                content=[TextBlock(text=assistant_text)],
            ),
        ]
        append_messages(turn_messages)
        recent.extend(turn_messages)
        await memory.write(
            MemoryWrite(
                kind="message",
                content=f"user: {turn_text}\nassistant: {assistant_text}",
            ),
            ctx,
        )
        turn_count += 1
        if memory.consolidation_due(turn_count):
            await memory.consolidate(make_ctx(turn_count, recent))
            recent.clear()

    assert output is not None  # turns is never empty
    for field_name, value in _output_delta(output, view).items():
        # Message-carrier fields were already threaded per turn.
        if field_name not in message_fields:
            write_field(field_name, value)
    return delta, output.model_dump(mode="json")


# --- function-node step ----------------------------------------------------------------


async def run_function_step(
    compiled: CompiledProject,
    function: CompiledFunction,
    run_state: dict[str, Any],
    session: Session,
    emitter: EventEmitter,
) -> dict[str, Any]:
    """One function-node step → state delta. Same state-visibility /
    retry / timeout / observability plumbing as agents, no LLM (docs/21
    § Function nodes)."""
    view = compiled.compiled_state.agent_views[function.name]
    emitter.emit(
        FunctionNodeStarted,
        node_name=function.name,
        node_version=function.node_version,
    )
    started = time.monotonic()
    state_view = view.project_input(run_state)
    ctx = RunContext(
        run_id=str(session.run_id),
        agent_name=function.name,
        session=session,
        tool_ref=f"function/{function.name}@{function.node_version}",
        timeout_s=function.spec.timeout_s,
        retry_policy=function.spec.retry_policy,
        connections=None,
        retrievers=None,
    )
    result = await _run_function_with_retries(function, state_view, ctx)
    if not isinstance(result, dict):
        raise OrchestrationError(
            f"function node {function.name!r} must return a dict state "
            f"delta; got {type(result).__name__}",
            context={"function": function.name,
                     "returned_type": type(result).__name__},
        )
    dropped = sorted(set(result) - set(view.write))
    delta = {k: v for k, v in result.items() if k in view.write}
    if dropped:
        emitter.emit(
            WarningEvent,
            agent_name=function.name,
            category="function_node.out_of_scope_write",
            message=f"function node {function.name!r} returned field(s) "
            f"outside its write scope; dropped: {', '.join(dropped)} "
            f"(write scope: {', '.join(view.write) or '(none)'})",
            error_class=None,
        )
    emitter.emit(
        FunctionNodeCompleted,
        node_name=function.name,
        node_version=function.node_version,
        fields_written=sorted(delta),
        bytes_delta=len(json.dumps(delta, default=str).encode()),
        latency_ms=int((time.monotonic() - started) * 1000),
    )
    return delta


async def _run_function_with_retries(
    function: CompiledFunction,
    state_view: dict[str, Any],
    ctx: RunContext,
) -> Any:
    policy = function.spec.retry_policy
    attempt = 0
    while True:
        attempt += 1
        try:
            async with asyncio.timeout(function.spec.timeout_s):
                return await function.handler(dict(state_view), ctx)
        except TimeoutError as exc:
            raise OrchestrationError(
                f"function node {function.name!r} exceeded its timeout of "
                f"{function.spec.timeout_s}s",
                context={"function": function.name,
                         "timeout_s": function.spec.timeout_s},
                cause=exc,
            ) from exc
        except FoundryError as exc:
            retryable = type(exc).__name__ in policy.retryable_errors
            if not retryable or attempt >= policy.max_attempts:
                raise
            await asyncio.sleep(policy.delay_for(attempt))
        except Exception as exc:  # wrap: arbitrary exceptions never escape
            raise OrchestrationError(
                f"function node {function.name!r} raised "
                f"{type(exc).__name__}: {exc}",
                context={"function": function.name,
                         "cause_type": type(exc).__name__},
                cause=exc,
            ) from exc


__all__ = [
    "EventEmitter",
    "EventSink",
    "RunCounters",
    "apply_delta",
    "build_messages",
    "llm_tool_loop",
    "parse_output",
    "run_agent_step",
    "run_function_step",
    "seed_state",
    "tool_descriptions",
    "tool_schemas",
]
