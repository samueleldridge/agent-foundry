"""LangGraph runtime adapter — compile + run a single-node graph.

The ONLY module (with ``_langgraph_types``) allowed to import ``langgraph`` /
``langchain_core`` (import-boundary lint). Phase 1 scope: compile a
``SystemSpec`` with a ``single`` flow into a one-node ``StateGraph`` whose node
makes one provider call and validates the response against the agent's output
schema. Checkpointers, streaming, and multi-node flows land in Phase 3.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from langgraph.graph import END, START, StateGraph
from pydantic import BaseModel, ValidationError

from foundry.config import EnvSecretsProvider, LoadedAgent, LoadedProject, load_project
from foundry.config.secrets import SecretsProvider
from foundry.core import (
    AgentCompleted,
    AgentStarted,
    FoundryMessage,
    LLMCallCompleted,
    LLMCallStarted,
    MessageRole,
    ModelResponse,
    RunCompleted,
    RunFailed,
    RunStarted,
    Session,
    TextBlock,
)
from foundry.core.errors import (
    CompileError,
    FoundryError,
    OrchestrationError,
    ProviderConfigError,
)
from foundry.providers import ProviderAdapter, resolve
from foundry.runtime._langgraph_types import GraphState

EventSink = Callable[[BaseModel], None]


# --- compilation -----------------------------------------------------------------


@dataclass(frozen=True)
class CompiledProject:
    """A Phase 1 compiled system: one agent, one provider, one output schema."""

    project: LoadedProject
    agent_name: str
    agent: LoadedAgent
    output_model: type[BaseModel]
    provider: ProviderAdapter
    pin_set_hash: str
    system_version: str


def compile_project(
    project_dir: Path,
    *,
    secrets: SecretsProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> CompiledProject:
    """Load + validate a project and resolve its provider.

    Raises ConfigError subclasses for bad YAML, CompileError for structural
    problems, ProviderConfigError / ProviderAuthError for binding problems.
    """
    project = load_project(project_dir)
    flow = project.system.flow
    if flow.type != "single":
        raise CompileError(
            f"Phase 1 supports only the 'single' flow pattern; "
            f"got {flow.type!r} (multi-node flows land in Phase 3+)",
            context={"file": str(project.directory / "system.yaml"),
                     "pointer": "/flow/type", "received": flow.type},
        )
    agent_name = flow.agent
    if agent_name not in project.agents:
        raise CompileError(
            f"flow.agent {agent_name!r} is not in SystemSpec.agents "
            f"{sorted(project.agents)}",
            context={"file": str(project.directory / "system.yaml"),
                     "pointer": "/flow/agent", "received": agent_name},
        )
    agent = project.agents[agent_name]
    agent_yaml = agent.directory / "agent.yaml"

    output_model = _load_output_schema(agent.directory, agent.spec.output.schema_ref)

    try:
        provider = resolve(
            agent.spec.model_binding,
            secrets or EnvSecretsProvider(),
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
    )


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
    compiled: CompiledProject, input_data: dict[str, Any]
) -> list[FoundryMessage]:
    schema_json = json.dumps(compiled.output_model.model_json_schema(), indent=2)
    system_text = (
        compiled.agent.prompt_text.rstrip()
        + "\n\nRespond ONLY with a single JSON object that validates against "
        "this JSON Schema — no code fences, no commentary:\n"
        + schema_json
    )
    return [
        FoundryMessage(role=MessageRole.SYSTEM, content=[TextBlock(text=system_text)]),
        FoundryMessage(
            role=MessageRole.USER,
            content=[TextBlock(text=json.dumps(input_data))],
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


async def run_project(
    compiled: CompiledProject,
    input_data: dict[str, Any],
    session: Session,
    event_sink: EventSink | None = None,
) -> RunResult:
    """Run the compiled single-agent system through a LangGraph StateGraph.

    Emits RunStarted → AgentStarted → LLMCallStarted/Completed →
    AgentCompleted → RunCompleted (or RunFailed on error, then re-raises).
    """
    emitter = _EventEmitter(session, event_sink)
    started = datetime.now(UTC)
    last_response: ModelResponse | None = None

    async def agent_node(state: GraphState) -> dict[str, Any]:
        nonlocal last_response
        spec = compiled.agent.spec
        emitter.emit(
            AgentStarted,
            agent_name=compiled.agent_name,
            agent_version=spec.prompt.version,
        )
        messages = _build_messages(compiled, state.get("input", {}))
        emitter.emit(
            LLMCallStarted,
            agent_name=compiled.agent_name,
            provider=compiled.provider.name,
            model=compiled.provider.model,
        )
        response = await compiled.provider.generate(
            messages,
            [],
            spec.model_binding.settings,
            session,
        )
        last_response = response
        emitter.emit(
            LLMCallCompleted,
            agent_name=compiled.agent_name,
            usage=response.usage,
            cost_estimate_usd=response.cost_estimate_usd,
            latency_ms=response.latency_ms,
            stop_reason=response.stop_reason,
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
        final_state = await app.ainvoke({"input": input_data})
    except FoundryError as exc:
        emitter.emit(RunFailed, error=exc.to_dict())
        raise
    except Exception as exc:  # wrap: no arbitrary exceptions cross the boundary
        wrapped = OrchestrationError(
            f"run failed with an unclassified error: {exc}",
            context={"project": compiled.project.system.name},
            cause=exc,
        )
        emitter.emit(RunFailed, error=wrapped.to_dict())
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
    return RunResult(output=final_state.get("output"), response=last_response)


__all__ = [
    "CompiledProject",
    "RunResult",
    "compile_project",
    "run_project",
]
