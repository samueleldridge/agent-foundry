"""Memory protocols (docs/10 § Memory, docs/26).

The concrete coordinator (``DefaultMemory``) and the three standard layers
live in ``foundry.memory``; this module holds only the protocol shapes and
the envelope/contribution/write value types so config schemas and consumers
depend on stable core types.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from foundry.core.messages import FoundryMessage
from foundry.core.retrieval import RetrievedDocument
from foundry.core.session import Session
from foundry.core.types import RunId

LayerKind = Literal["working", "episodic", "semantic", "custom"]

StateWriter = Callable[[str, Any], None]
"""Write one state field on the agent's behalf. The runtime supplies a
writer that enforces the agent's write scope + reducers; layers never touch
state directly."""


class MemoryContext(BaseModel):
    """What every memory operation receives (docs/26 § Lifecycle)."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    run_id: RunId
    agent_name: str
    session: Session
    state_view: dict[str, Any] = Field(default_factory=dict)
    """The agent's read-scope projection of state at this turn."""
    state_writer: StateWriter | None = None
    """None when the agent has no writable memory fields."""
    turn_count: int = 0
    """Completed turns so far in this run (consolidation cadence input)."""
    recent_messages: list[FoundryMessage] = Field(default_factory=list)
    """Turn messages since the last consolidation — the consolidator's
    {recent_messages} carrier."""


class MemoryContribution(BaseModel):
    model_config = ConfigDict(frozen=True)

    layer_name: str
    layer_kind: LayerKind
    content: list[FoundryMessage] | list[RetrievedDocument] | str
    tokens_estimate: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemoryEnvelope(BaseModel):
    model_config = ConfigDict(frozen=True)

    contributions: list[MemoryContribution] = Field(default_factory=list)
    total_tokens_estimate: int = 0
    truncated: bool = False
    layers_truncated: list[str] = Field(default_factory=list)
    """Layers whose contribution was cut by max_envelope_tokens —
    last-listed first (docs/26 § Prompt assembly rule 6)."""
    layers_failed: list[str] = Field(default_factory=list)
    """Layers that degraded to an empty contribution (fail-open default)."""


class MemoryWrite(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["message", "summary", "fact", "raw"]
    content: str | FoundryMessage
    target_layer: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class MemoryLayer(Protocol):
    kind: LayerKind
    name: str

    async def read(self, query: str, ctx: MemoryContext) -> MemoryContribution: ...
    async def write(self, content: MemoryWrite, ctx: MemoryContext) -> None: ...
    async def consolidate(self, ctx: MemoryContext) -> None: ...


@runtime_checkable
class Memory(Protocol):
    layers: list[MemoryLayer]

    async def read(self, query: str, ctx: MemoryContext) -> MemoryEnvelope: ...
    async def write(self, content: MemoryWrite, ctx: MemoryContext) -> None: ...
    async def consolidate(self, ctx: MemoryContext) -> None: ...


__all__ = [
    "LayerKind",
    "Memory",
    "MemoryContext",
    "MemoryContribution",
    "MemoryEnvelope",
    "MemoryLayer",
    "MemoryWrite",
    "StateWriter",
]
