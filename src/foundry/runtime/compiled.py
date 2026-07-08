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
class CompiledProject:
    """A Phase 2 compiled system: one agent (plus any function nodes in a
    sequential flow), its tools + connections + state + caches + memory."""

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


__all__ = [
    "CompileWarning",
    "CompiledFunction",
    "CompiledProject",
    "FunctionHandler",
    "RunResult",
]
