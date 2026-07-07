"""FunctionNode protocol + BaseFunctionNode.

A FunctionNode is a deterministic Python node — same flow position as an
Agent but no LLM. Used for input normalisation, output formatting, etc.
See docs/10 § The FunctionNode protocol.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from foundry.core.agent import LifecycleHooks
from foundry.core.node import NodeResult
from foundry.core.session import Session
from foundry.core.state import StateBase


@runtime_checkable
class FunctionNode(Protocol):
    name: str
    version: str

    async def run(self, state: StateBase, session: Session) -> NodeResult: ...


class BaseFunctionNode:
    """Convenience base for hand-written function nodes."""

    def __init__(
        self,
        *,
        name: str,
        version: str,
        hooks: LifecycleHooks | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.name = name
        self.version = version
        self.timeout_s = timeout_s
        self._hooks = hooks or LifecycleHooks()

    async def run(self, state: StateBase, session: Session) -> NodeResult:
        async with session.span(
            "foundry.function_node", node=self.name, version=self.version
        ):
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

    async def _step(self, state: StateBase, session: Session) -> NodeResult:
        raise NotImplementedError


__all__ = ["BaseFunctionNode", "FunctionNode"]
