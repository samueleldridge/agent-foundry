"""Agent protocol, BaseAgent, LifecycleHooks.

See docs/10 § The ``Agent`` protocol. Phase 1 ships the protocol + BaseAgent
with no-op-by-default hooks; subsequent phases wire the hooks into the
LangGraph adapter (Phase 3) and meta-agent (Phase 6).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from foundry.core.node import NodeResult
from foundry.core.session import Session
from foundry.core.state import StateBase


class AgentResult(NodeResult):
    """Result of a single agent step. Inherits state_delta / output / next."""


@runtime_checkable
class Agent(Protocol):
    """Root agent abstraction.

    Implementations produce a state delta and/or a final output given the
    current state and a session. Agents are stateless across runs.
    """

    name: str
    version: str

    async def run(self, state: StateBase, session: Session) -> AgentResult: ...


# --- LifecycleHooks --------------------------------------------------------


_BeforeRun = Callable[[Session], Awaitable[None]]
_AfterRun = Callable[[Session, AgentResult | None], Awaitable[None]]
_BeforeNode = Callable[[Any, StateBase, Session], Awaitable[None]]
_AfterNode = Callable[[Any, NodeResult, StateBase, Session], Awaitable[None]]
_OnError = Callable[[Any, Exception, Session], Awaitable[None]]
_BeforeTool = Callable[[Any, Any, Session], Awaitable[None]]
_AfterTool = Callable[[Any, Any, Any, Session], Awaitable[None]]


class LifecycleHooks(BaseModel):
    """Optional pre/post/error hooks.

    Hooks are instrumentation, not business logic. They never swallow
    exceptions; they log and re-raise. Methods are no-ops when the
    corresponding callable is ``None``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True)

    before_run: _BeforeRun | None = None
    after_run: _AfterRun | None = None
    before_node: _BeforeNode | None = None
    after_node: _AfterNode | None = None
    on_error: _OnError | None = None
    before_tool: _BeforeTool | None = None
    after_tool: _AfterTool | None = None


# --- BaseAgent -------------------------------------------------------------


class BaseAgent:
    """Convenience base for hand-written agents.

    Subclasses override ``_step``. The base wraps the step in a span, fires
    lifecycle hooks, and translates exceptions through ``on_error``.
    """

    def __init__(
        self,
        *,
        name: str,
        version: str,
        hooks: LifecycleHooks | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self._hooks = hooks or LifecycleHooks()

    async def run(self, state: StateBase, session: Session) -> AgentResult:
        async with session.span("foundry.node", agent=self.name, version=self.version):
            if self._hooks.before_node is not None:
                await self._hooks.before_node(self, state, session)
            try:
                result = await self._step(state, session)
            except Exception as exc:
                if self._hooks.on_error is not None:
                    await self._hooks.on_error(self, exc, session)
                raise
            if self._hooks.after_node is not None:
                await self._hooks.after_node(self, result, state, session)
            return result

    async def _step(self, state: StateBase, session: Session) -> AgentResult:
        raise NotImplementedError


__all__ = ["Agent", "AgentResult", "BaseAgent", "LifecycleHooks"]
