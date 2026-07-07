"""Memory protocols — Phase 1 type stubs.

Concrete memory coordinator + three standard layers land in Phase 2c. Phase 1
ships the protocol shapes + envelope/contribution/write so config schemas
(MemoryConfig) and the public ``core`` re-export are stable.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from foundry.core.messages import FoundryMessage
from foundry.core.retrieval import RetrievedDocument
from foundry.core.types import RunId

LayerKind = Literal["working", "episodic", "semantic", "custom"]


class MemoryContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    run_id: RunId
    agent_name: str
    session: Any  # avoid circular import; concrete typing in Phase 2c
    state_view: dict[str, Any] = Field(default_factory=dict)
    state_writer: Any = None


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
    "Memory",
    "MemoryContext",
    "MemoryContribution",
    "MemoryEnvelope",
    "MemoryLayer",
    "MemoryWrite",
]
