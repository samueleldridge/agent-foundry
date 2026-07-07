"""BaseAgent lifecycle + protocol compliance tests (docs/10 § Agent)."""

from __future__ import annotations

import pytest

from foundry.core import (
    Agent,
    AgentResult,
    BaseAgent,
    BaseFunctionNode,
    LifecycleHooks,
    Node,
    NodeResult,
    Session,
    StateBase,
)


class _EchoAgent(BaseAgent):
    async def _step(self, state: StateBase, session: Session) -> AgentResult:
        return AgentResult(state_delta={"echo": True}, output="done")


class _BoomAgent(BaseAgent):
    async def _step(self, state: StateBase, session: Session) -> AgentResult:
        raise RuntimeError("boom")


class _PassNode(BaseFunctionNode):
    async def _step(self, state: StateBase, session: Session) -> NodeResult:
        return NodeResult(state_delta={"normalised": True})


@pytest.mark.unit
def test_protocol_compliance() -> None:
    agent = _EchoAgent(name="echo", version="v1")
    node = _PassNode(name="pass", version="v1")
    assert isinstance(agent, Agent)
    assert isinstance(agent, Node)
    assert isinstance(node, Node)


@pytest.mark.unit
async def test_lifecycle_hooks_fire_in_order() -> None:
    calls: list[str] = []

    async def before_node(node: object, state: StateBase, session: Session) -> None:
        calls.append("before_node")

    async def after_node(
        node: object, result: NodeResult, state: StateBase, session: Session
    ) -> None:
        calls.append("after_node")

    async def on_error(node: object, exc: Exception, session: Session) -> None:
        calls.append("on_error")

    hooks = LifecycleHooks(
        before_node=before_node, after_node=after_node, on_error=on_error
    )
    agent = _EchoAgent(name="echo", version="v1", hooks=hooks)
    session = Session.new(project="test")
    result = await agent.run(StateBase(), session)
    assert result.output == "done"
    assert calls == ["before_node", "after_node"]


@pytest.mark.unit
async def test_on_error_hook_fires_and_reraises() -> None:
    calls: list[str] = []

    async def on_error(node: object, exc: Exception, session: Session) -> None:
        calls.append(f"on_error:{exc}")

    agent = _BoomAgent(name="boom", version="v1", hooks=LifecycleHooks(on_error=on_error))
    session = Session.new(project="test")
    with pytest.raises(RuntimeError, match="boom"):
        await agent.run(StateBase(), session)
    assert calls == ["on_error:boom"]
