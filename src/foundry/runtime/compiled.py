"""Compiled-project value types shared by the runtime modules.

No langgraph imports here — the import boundary confines those to
``langgraph_adapter`` / ``_langgraph_types``.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from foundry.cache import PreparedSemanticCache
from foundry.config import (
    EnvSecretsProvider,
    FoundryRoots,
    FunctionNodeSpec,
    LoadedAgent,
    LoadedProject,
)
from foundry.config.secrets import SecretsProvider
from foundry.connections import PreparedConnection
from foundry.core import ModelResponse, ToolRegistry
from foundry.core.tool import RunContext
from foundry.memory import PreparedMemory
from foundry.orchestration.handoff import HandoffTool
from foundry.orchestration.patterns import FlowPlan, LeafNode
from foundry.orchestration.state_scope import CompiledState
from foundry.providers import ProviderAdapter
from foundry.retrieval import PreparedRetriever

FunctionHandler = Callable[[dict[str, Any], RunContext], Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class CompileWarning:
    """A non-fatal compile-time finding. Printed by the CLI at compile and
    re-emitted as a WarningEvent at run start so the audit trail records it."""

    agent_name: str
    category: str
    message: str


@dataclass(frozen=True)
class CompiledFunction:
    """A fully-resolved function node: spec + imported handler + content-
    hashed node_version (over function source + config, docs/21)."""

    name: str
    spec: FunctionNodeSpec
    handler: FunctionHandler
    node_version: str
    directory: Path


@dataclass(frozen=True)
class CompiledAgent:
    """One fully-resolved agent node: spec + prompt + provider + output
    schema + its own retrievers / semantic cache / memory, plus any
    compiler-synthesised handoff tools (supervisors only)."""

    name: str
    loaded: LoadedAgent
    output_model: type[BaseModel]
    provider: ProviderAdapter
    retrievers: dict[str, PreparedRetriever] = field(default_factory=dict)
    semantic_cache: PreparedSemanticCache | None = None
    memory: PreparedMemory | None = None
    handoff_tools: tuple[HandoffTool, ...] = ()
    """Non-empty only for a supervisor: the compile-generated
    ``transfer_to_*`` tool set (docs/30 § Handoff tool generation)."""


@dataclass(frozen=True)
class CompiledProject:
    """A compiled system: agents (one per flow node), function nodes,
    tools + connections + state + caches + memory, and the flow plan.

    The single-agent fields (``agent_name`` / ``agent`` / ``output_model``
    / ``provider`` / ``retrievers`` / ``semantic_cache`` / ``memory``)
    mirror the PRIMARY (terminal/output) agent so every Phase 1-6 call
    site keeps working; ``compiled_agents`` + ``plan`` carry the Phase 7
    multi-agent shape."""

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
    functions: dict[str, CompiledFunction] = field(default_factory=dict)
    """Function nodes by logical name (Phase 2c)."""
    flow_steps: tuple[str, ...] = ()
    """Execution order for a sequential flow; () for single flows."""
    memory: PreparedMemory | None = None
    """The flow agent's validated memory config (None = memory off)."""
    compile_warnings: tuple[CompileWarning, ...] = ()
    """Non-fatal compile-time findings (e.g. semantic-cache bypass)."""
    compiled_agents: dict[str, CompiledAgent] = field(default_factory=dict)
    """Every agent by name (Phase 7). Empty for synthetic single-agent
    projects built outside the compiler (the meta-agent) — use
    :meth:`agent_map`, which synthesises the primary entry."""
    plan: FlowPlan | None = None
    """The compiled flow plan (Phase 7). None for synthetic projects —
    use :meth:`flow_plan`."""

    def agent_map(self) -> dict[str, CompiledAgent]:
        """``compiled_agents``, or the legacy single-agent synthesis."""
        if self.compiled_agents:
            return self.compiled_agents
        return {
            self.agent_name: CompiledAgent(
                name=self.agent_name,
                loaded=self.agent,
                output_model=self.output_model,
                provider=self.provider,
                retrievers=self.retrievers,
                semantic_cache=self.semantic_cache,
                memory=self.memory,
            )
        }

    def flow_plan(self) -> FlowPlan:
        """``plan``, or the legacy single/sequential synthesis."""
        if self.plan is not None:
            return self.plan
        if self.flow_steps:
            from foundry.orchestration.patterns import SequentialPlan

            steps = tuple(
                LeafNode(
                    kind="agent" if step == self.agent_name else "function",
                    name=step,
                )
                for step in self.flow_steps
            )
            return FlowPlan(
                pattern="sequential",
                root=SequentialPlan(name="", steps=steps),
                primary_agent=self.agent_name,
                agents=(self.agent_name,),
                subflow_names=(),
                steps=self.flow_steps,
            )
        return FlowPlan(
            pattern="single",
            root=LeafNode(kind="agent", name=self.agent_name),
            primary_agent=self.agent_name,
            agents=(self.agent_name,),
            subflow_names=(),
        )

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


@dataclass(frozen=True)
class RunResult:
    output: Any
    response: ModelResponse | None
    pool_metrics: dict[str, Any] = field(default_factory=dict)
    llm_call_count: int = 0
    """Actual provider calls made — 0 on a semantic-cache hit."""
    final_state: dict[str, Any] | None = None
    """The run's final state projection (written to final_state.json)."""
    resumed: bool = False
    """True when this invocation resumed an interrupted checkpointed run."""
    status: str = "success"
    """'success' | 'approval_pending' | 'max_hops' (return_partial)."""
    pending_approval: dict[str, Any] | None = None
    """The InterruptPayload dict while status == 'approval_pending'."""


__all__ = [
    "CompileWarning",
    "CompiledAgent",
    "CompiledFunction",
    "CompiledProject",
    "FunctionHandler",
    "RunResult",
]
