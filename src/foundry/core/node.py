"""Node protocol — shared parent of Agent and FunctionNode.

The orchestration compiler accepts a ``dict[str, Node]`` registry and doesn't
care which kind each entry is. See docs/10 § Node protocol.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from foundry.core.session import Session
from foundry.core.state import StateBase


class NodeResult(BaseModel):
    """Common result shape for both Agent and FunctionNode."""

    model_config = ConfigDict(extra="forbid")

    state_delta: dict[str, Any] = Field(default_factory=dict)
    output: Any | None = None
    next: str | Literal["END"] | None = None


@runtime_checkable
class Node(Protocol):
    name: str
    version: str

    async def run(self, state: StateBase, session: Session) -> NodeResult: ...


__all__ = ["Node", "NodeResult"]
