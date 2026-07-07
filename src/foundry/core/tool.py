"""Tool protocol — Phase 1 stub.

The Tool protocol's full surface (BaseTool, ToolRegistry, RunContext with
ConnectionAccessor) is exercised starting in Phase 2a. Phase 1 ships only
the protocol shape + RetryPolicy so config schemas and the public ``core``
re-export are stable.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from foundry.core.session import Session


class BackoffStrategy(StrEnum):
    CONSTANT = "constant"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1, le=20)
    backoff: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    initial_delay_s: float = Field(default=1.0, gt=0)
    max_delay_s: float = Field(default=30.0, gt=0)
    retryable_errors: list[str] = Field(
        default_factory=lambda: ["ProviderRateLimitError", "ProviderTimeoutError"]
    )
    jitter: bool = True


@runtime_checkable
class Tool(Protocol):
    name: str
    version: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]

    async def handle(self, inputs: BaseModel, ctx: RunContext) -> BaseModel: ...


class RunContext(BaseModel):
    """Handle threaded into tool handlers.

    Phase 1 surface is minimal; ConnectionAccessor lands in Phase 2a.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    run_id: str
    agent_name: str
    session: Session
    tool_ref: str
    timeout_s: float | None = None
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)


class BaseTool:
    """Convenience base for hand-written tools (Phase 2a).

    Phase 1 ships a structural shell only so ``BaseTool`` is importable from
    ``foundry.core``. Subclasses must define ``input_schema`` / ``output_schema``
    and implement ``handle``.
    """

    name: str = ""
    version: str = ""
    input_schema: type[BaseModel] = BaseModel
    output_schema: type[BaseModel] = BaseModel

    async def handle(self, inputs: BaseModel, ctx: RunContext) -> BaseModel:
        raise NotImplementedError


class ToolRegistry:
    """Name+version indexed tool lookup. Phase 2a populates this; Phase 1
    ships the type stub so the public re-export is stable."""

    def __init__(self) -> None:
        self._tools: dict[tuple[str, str], Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[(tool.name, tool.version)] = tool

    def get(self, name: str, version: str) -> Tool | None:
        return self._tools.get((name, version))

    def __contains__(self, key: tuple[str, str]) -> bool:
        return key in self._tools


__all__ = [
    "BackoffStrategy",
    "BaseTool",
    "RetryPolicy",
    "RunContext",
    "Tool",
    "ToolRegistry",
]
