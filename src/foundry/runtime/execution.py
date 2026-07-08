"""Step execution: node-sized slices of the agent step (with optional
memory) + the function-node step + shared event emission.

Phase 3 shape: :class:`AgentStepRuntime` splits the agent step into graph-
node-sized async methods (``begin`` → ``llm_round`` ⇄ ``dispatch_tools`` →
``finish``, with ``start_turn`` / ``end_turn`` wrapping the loop for
memory-enabled agents). The LangGraph adapter wires them as StateGraph
nodes, so the LLM ⇄ tool loop and the docs/26 memory turn loop are
checkpointed at every boundary — a killed run resumes mid-loop instead of
restarting the whole step.

No langgraph imports — graph wiring lives in ``langgraph_adapter``; every
method here takes plain ``(conv, run_state)`` dicts and returns a partial
graph-state update (keys: ``conv`` / ``state`` / ``output``).
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
from foundry.observability.tracing import foundry_span, set_span_attributes
from foundry.orchestration.state_scope import AgentStateView
from foundry.providers import ToolSchema
from foundry.retrieval import MappingRetrieverAccessor
from foundry.runtime.compiled import CompiledFunction, CompiledProject

EventSink = Callable[[BaseModel], None]

NodeUpdate = dict[str, Any]
"""A partial graph-state update: any of ``conv`` / ``state`` / ``output``."""

_TURNS_FIELD = "turns"
"""Phase 2c multi-turn convention: a memory-enabled agent whose read scope
projects a list-of-strings field named ``turns`` converses once per item.
Phase 3 checkpoints the loop (kill mid-turn → resume); the API-level
conversation surface remains Phase 8."""


class EventEmitter:
    """Sequence-stamped event emission (event-stream invariant 1).

    ``start_sequence`` lets a resumed run continue the sequence where the
    killed process stopped (SSE Last-Event-ID semantics, docs/10)."""

    def __init__(
        self,
        session: Session,
        sink: EventSink | None,
        *,
        start_sequence: int = 0,
    ) -> None:
        self._session = session
        self._sink = sink
        self._sequence = start_sequence

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


# --- agent step (node-sized slices) ---------------------------------------------------


def _output_delta(
    output: BaseModel, view: AgentStateView
) -> dict[str, Any]:
    """Project the agent's output model onto its write scope. Out-of-scope
    output fields are normal for agents (the output schema is the caller's
    contract; state gets the declared projection) — no warning, unlike
    function nodes whose return value IS a state delta."""
    dump = output.model_dump(mode="json")
    return {k: v for k, v in dump.items() if k in view.write}


def _extract_turns(agent_input: dict[str, Any]) -> list[str]:
    raw = agent_input.get(_TURNS_FIELD)
    if isinstance(raw, list) and raw and all(isinstance(t, str) for t in raw):
        return list(raw)
    return [json.dumps(agent_input)]


def _response_text(response: ModelResponse) -> str:
    return "".join(
        b.text for b in response.message.content if isinstance(b, TextBlock)
    ).strip()


class AgentStepRuntime:
    """The agent step, sliced into graph nodes.

    Routing vocabulary returned by the ``route_after_*`` methods (the
    adapter maps labels onto graph node names):

    - ``begin`` → ``turn`` (memory) | ``llm`` | ``finish`` (cache hit)
    - ``llm``   → ``tools`` (tool_use blocks) | ``turn_end`` (memory) |
      ``finish``
    - ``tools`` → ``llm`` (unconditional loop edge)
    - ``turn_end`` → ``turn`` (more turns) | ``finish``

    All conversation state lives in the ``conv`` dict inside the graph
    state (checkpointed); this object holds only process-scoped plumbing
    (provider, pool, emitter, lazily-built memory coordinator) so a fresh
    process can resume a checkpointed conv.
    """

    def __init__(
        self,
        compiled: CompiledProject,
        session: Session,
        emitter: EventEmitter,
        pool: InProcessConnectionPool,
        counters: RunCounters,
    ) -> None:
        self.compiled = compiled
        self.session = session
        self.emitter = emitter
        self.pool = pool
        self.counters = counters
        self.retrievers: MappingRetrieverAccessor | None = None
        self._memory_obj: DefaultMemory | None = None
        self.view = compiled.compiled_state.agent_views[compiled.agent_name]
        self.reducers = compiled.compiled_state.reducers
        self.schemas = tool_schemas(compiled)
        self.descriptions = tool_descriptions(self.schemas)
        self.message_fields: list[str] = []
        if compiled.memory is not None:
            memory_config = compiled.memory.spec.memory
            assert memory_config is not None
            schema_types = {
                name: field_spec.type.replace(" ", "")
                for name, field_spec in (
                    compiled.project.state.state_schema.items()
                )
            }
            self.message_fields = [
                layer.source_field
                for layer in memory_config.layers
                if isinstance(layer, WorkingMemoryLayerConfig)
                and layer.source_field in self.view.write
                and schema_types.get(layer.source_field) == "list[FoundryMessage]"
            ]

    # --- process-scoped helpers -------------------------------------------------

    def _memory(self) -> DefaultMemory:
        """Built lazily: a resumed process may enter mid-turn without ever
        running ``begin``, and the retriever accessor is attached to this
        runtime only just before graph invocation."""
        assert self.compiled.memory is not None
        if self._memory_obj is None:
            self._memory_obj = build_memory(
                self.compiled.memory,
                agent_name=self.compiled.agent_name,
                provider=self.compiled.provider,
                retrievers=self.retrievers,
                emit=self.emitter.emit,
            )
        return self._memory_obj

    def _make_ctx(
        self,
        local: dict[str, Any],
        writes: dict[str, Any],
        turn_count: int,
        recent: list[FoundryMessage],
    ) -> MemoryContext:
        def write_field(field_name: str, value: Any) -> None:
            if field_name not in self.view.write:
                self.emitter.emit(
                    WarningEvent,
                    agent_name=self.compiled.agent_name,
                    category="memory.out_of_scope_write",
                    message=(
                        f"memory write to state field {field_name!r} dropped: "
                        f"outside agent {self.compiled.agent_name!r}'s write "
                        "scope"
                    ),
                    error_class=None,
                )
                return
            local[field_name] = value
            writes[field_name] = value

        return MemoryContext(
            run_id=self.session.run_id,
            agent_name=self.compiled.agent_name,
            session=self.session,
            state_view=self.view.project_input(local),
            state_writer=write_field,
            turn_count=turn_count,
            recent_messages=list(recent),
        )

    # --- nodes -------------------------------------------------------------------

    async def begin(
        self, conv: dict[str, Any], run_state: dict[str, Any]
    ) -> NodeUpdate:
        """Emit agent.started; seed the conversation bundle. Non-memory
        agents also consult the semantic cache here (docs/24 § Layer 2:
        keyed by the step's INITIAL input; a hit skips the whole loop)."""
        spec = self.compiled.agent.spec
        self.emitter.emit(
            AgentStarted,
            agent_name=self.compiled.agent_name,
            agent_version=spec.prompt.version,
        )
        if self.compiled.memory is not None:
            turns = _extract_turns(self.view.project_input(run_state))
            return {
                "conv": {
                    "mode": "memory",
                    "turns": turns,
                    "turn_index": 0,
                    "recent": [],
                    "messages": [],
                    "round": 0,
                    "response": None,
                    "output_dump": None,
                }
            }
        agent_input = self.view.project_input(run_state)
        messages = build_messages(self.compiled, agent_input, self.descriptions)
        new_conv: dict[str, Any] = {
            "mode": "plain",
            "messages": messages,
            "round": 0,
            "response": None,
            "cache_hit": False,
            "cache_key": None,
        }
        semantic = self.compiled.semantic_cache
        if semantic is not None:
            await ensure_version_marker(
                semantic, self.compiled.agent_name, self.emitter.emit
            )
            response, cache_key = await semantic_lookup(
                semantic,
                agent_name=self.compiled.agent_name,
                model_binding=spec.model_binding,
                tools=self.schemas,
                messages=messages,
                emit=self.emitter.emit,
            )
            new_conv["response"] = response
            new_conv["cache_hit"] = response is not None
            new_conv["cache_key"] = cache_key
        return {"conv": new_conv}

    def route_after_begin(self, conv: dict[str, Any]) -> str:
        if conv.get("mode") == "memory":
            return "turn"
        return "finish" if conv.get("response") is not None else "llm"

    async def start_turn(
        self, conv: dict[str, Any], run_state: dict[str, Any]
    ) -> NodeUpdate:
        """docs/26 per-turn head: memory.read → weave → turn messages."""
        assert self.compiled.memory is not None
        memory_config = self.compiled.memory.spec.memory
        assert memory_config is not None
        turn_text: str = conv["turns"][conv["turn_index"]]
        local = dict(run_state)
        writes: dict[str, Any] = {}
        ctx = self._make_ctx(local, writes, conv["turn_index"], conv["recent"])
        envelope = await self._memory().read(turn_text, ctx)
        woven = weave(
            _system_text(self.compiled, self.descriptions), envelope, memory_config
        )
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
        update: NodeUpdate = {
            "conv": {**conv, "messages": messages, "round": 0, "response": None}
        }
        if writes:
            update["state"] = apply_delta(run_state, writes, self.reducers)
        return update

    async def llm_round(
        self, conv: dict[str, Any], run_state: dict[str, Any]
    ) -> NodeUpdate:
        """One LLM call, wrapped in a ``foundry.llm`` span (docs/01 attrs)."""
        spec = self.compiled.agent.spec
        if conv["round"] >= spec.iteration_limit:
            raise IterationLimitError(
                f"agent {self.compiled.agent_name!r} exceeded its "
                f"iteration_limit of {spec.iteration_limit} LLM rounds "
                "without a terminal response",
                context={"agent": self.compiled.agent_name,
                         "iteration_limit": spec.iteration_limit},
            )
        capture = self.compiled.project.system.observability.capture_inputs
        messages: list[FoundryMessage] = list(conv["messages"])
        self.emitter.emit(
            LLMCallStarted,
            agent_name=self.compiled.agent_name,
            provider=self.compiled.provider.name,
            model=self.compiled.provider.model,
            prompt_messages=list(messages) if capture else None,
        )
        with foundry_span(
            "foundry.llm",
            {
                "run_id": str(self.session.run_id),
                "project": self.compiled.project.system.name,
                "agent": self.compiled.agent_name,
                "provider": self.compiled.provider.name,
                "model": self.compiled.provider.model,
                "tool_schemas_count": len(self.schemas),
            },
        ) as span:
            response = await self.compiled.provider.generate(
                messages,
                self.schemas,
                spec.model_binding.settings,
                self.session,
            )
            set_span_attributes(
                span,
                {
                    "prompt_tokens": response.usage.input_tokens,
                    "completion_tokens": response.usage.output_tokens,
                    "latency_ms": response.latency_ms,
                    "cost_estimate_usd": response.cost_estimate_usd,
                    "stop_reason": response.stop_reason.value,
                },
            )
        self.counters.llm_call_count += 1
        self.counters.last_response = response
        self.emitter.emit(
            LLMCallCompleted,
            agent_name=self.compiled.agent_name,
            usage=response.usage,
            cost_estimate_usd=response.cost_estimate_usd,
            latency_ms=response.latency_ms,
            stop_reason=response.stop_reason,
        )
        return {"conv": {**conv, "round": conv["round"] + 1, "response": response}}

    def route_after_llm(self, conv: dict[str, Any]) -> str:
        response: ModelResponse = conv["response"]
        has_tool_uses = any(
            isinstance(b, ToolUseBlock) for b in response.message.content
        )
        if has_tool_uses:
            return "tools"
        return "turn_end" if conv.get("mode") == "memory" else "finish"

    async def dispatch_tools(
        self, conv: dict[str, Any], run_state: dict[str, Any]
    ) -> NodeUpdate:
        """Dispatch every tool_use block of the round in parallel (docs/21)
        and grow the conversation with the results."""
        response: ModelResponse = conv["response"]
        tool_uses = [
            b for b in response.message.content if isinstance(b, ToolUseBlock)
        ]
        results = list(
            await asyncio.gather(
                *(
                    _dispatch_one(
                        self.compiled, self.pool, self.session, self.emitter,
                        block, self.retrievers,
                    )
                    for block in tool_uses
                )
            )
        )
        messages = [
            *conv["messages"],
            response.message,
            FoundryMessage(role=MessageRole.USER, content=list(results)),
        ]
        return {"conv": {**conv, "messages": messages, "response": None}}

    async def end_turn(
        self, conv: dict[str, Any], run_state: dict[str, Any]
    ) -> NodeUpdate:
        """docs/26 per-turn tail: state append + memory.write (episodic
        ingest) → periodic consolidation."""
        response: ModelResponse = conv["response"]
        output = parse_output(self.compiled, response)
        turn_text: str = conv["turns"][conv["turn_index"]]
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
        local = dict(run_state)
        writes: dict[str, Any] = {}
        delta: dict[str, Any] = {}
        for field_name in self.message_fields:
            current = list(local.get(field_name) or [])
            local[field_name] = [*current, *turn_messages]
            if self.reducers.get(field_name) is Reducer.APPEND:
                delta[field_name] = list(turn_messages)
            else:
                delta[field_name] = local[field_name]
        ctx = self._make_ctx(local, writes, conv["turn_index"], conv["recent"])
        await self._memory().write(
            MemoryWrite(
                kind="message",
                content=f"user: {turn_text}\nassistant: {assistant_text}",
            ),
            ctx,
        )
        turn_count = conv["turn_index"] + 1
        recent: list[FoundryMessage] = [*conv["recent"], *turn_messages]
        if self._memory().consolidation_due(turn_count):
            await self._memory().consolidate(
                self._make_ctx(local, writes, turn_count, recent)
            )
            recent = []
        new_conv = {
            **conv,
            "turn_index": turn_count,
            "recent": recent,
            "messages": [],
            "response": None,
            "output_dump": output.model_dump(mode="json"),
        }
        return {
            "conv": new_conv,
            "state": apply_delta(run_state, {**delta, **writes}, self.reducers),
        }

    def route_after_turn_end(self, conv: dict[str, Any]) -> str:
        return "turn" if conv["turn_index"] < len(conv["turns"]) else "finish"

    async def finish(
        self, conv: dict[str, Any], run_state: dict[str, Any]
    ) -> NodeUpdate:
        """Terminal slice: semantic-cache store (miss path), output parse +
        write-scope projection, agent.completed."""
        if conv.get("mode") == "memory":
            output_dump: dict[str, Any] = conv["output_dump"]
            final_writes = {
                field_name: value
                for field_name, value in output_dump.items()
                if field_name in self.view.write
                and field_name not in self.message_fields
            }
            self.emitter.emit(
                AgentCompleted,
                agent_name=self.compiled.agent_name,
                output_summary=f"{self.compiled.output_model.__name__} produced",
            )
            return {
                "conv": None,
                "state": apply_delta(run_state, final_writes, self.reducers),
                "output": output_dump,
            }
        response: ModelResponse = conv["response"]
        semantic = self.compiled.semantic_cache
        if (
            semantic is not None
            and conv.get("cache_key") is not None
            and not conv.get("cache_hit")
        ):
            # Store the TERMINAL response keyed by the initial input — a
            # future hit replays the final answer without the tool loop.
            await semantic_store(
                semantic, conv["cache_key"], response,
                agent_name=self.compiled.agent_name, emit=self.emitter.emit,
            )
        output = parse_output(self.compiled, response)
        self.emitter.emit(
            AgentCompleted,
            agent_name=self.compiled.agent_name,
            output_summary=f"{self.compiled.output_model.__name__} produced",
        )
        return {
            "conv": None,
            "state": apply_delta(
                run_state, _output_delta(output, self.view), self.reducers
            ),
            "output": output.model_dump(mode="json"),
        }


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
    "AgentStepRuntime",
    "EventEmitter",
    "EventSink",
    "NodeUpdate",
    "RunCounters",
    "apply_delta",
    "build_messages",
    "parse_output",
    "run_function_step",
    "seed_state",
    "tool_descriptions",
    "tool_schemas",
]
