"""Handoff-tool generation (docs/30) + HITL payload shapes (docs/32) +
ApprovalRequired pass-through on the dispatch path."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from foundry.core import (
    RegisteredTool,
    RetryPolicy,
    Session,
    ToolDescriptor,
    ToolRegistry,
)
from foundry.core.errors import ApprovalRequired, CompileError
from foundry.core.events import ToolCompleted
from foundry.core.tool import RunContext
from foundry.orchestration.handoff import (
    HandoffInput,
    build_handoff_tools,
    check_no_user_handoff_tools,
    handoff_tool_name,
)
from foundry.orchestration.hitl import (
    interrupt_payload,
    parse_payload,
    parse_resolution,
    resolution_record,
)
from foundry.orchestration.patterns import SupervisorPlan

# --- handoff tool generation --------------------------------------------------


def _plan(targets: tuple[str, ...]) -> SupervisorPlan:
    return SupervisorPlan(
        name="",
        supervisor="orch",
        workers=tuple((t, object()) for t in targets if t != "END"),  # type: ignore[misc]
        supervisor_targets=targets,
        workers_may_end=frozenset(),
        termination_when=None,
        max_hops=10,
        on_max_hops="error",
        escalate_to=None,
    )


@pytest.mark.unit
def test_handoff_tools_generated_per_allowed_target() -> None:
    """docs/30 unit test 6: workers [a, b] produce transfer_to_a /
    transfer_to_b (+ transfer_to_end when END is allowed) with
    auto-generated descriptions."""
    tools = build_handoff_tools(
        _plan(("break_detector", "resolver", "END")),
        {"break_detector": "Finds breaks.", "resolver": "Fixes them."},
    )
    assert [t.name for t in tools] == [
        "transfer_to_break_detector",
        "transfer_to_resolver",
        "transfer_to_end",
    ]
    assert tools[0].target == "break_detector"
    assert "Finds breaks." in tools[0].description
    assert tools[2].target == "END"
    assert "final" in tools[2].description


@pytest.mark.unit
def test_no_end_tool_without_end_target() -> None:
    tools = build_handoff_tools(_plan(("a",)), {})
    assert [t.name for t in tools] == ["transfer_to_a"]


@pytest.mark.unit
def test_handoff_input_shape() -> None:
    """docs/30: reason min_length 10 — a lazy 'go' is rejected and the
    supervisor gets a structured validation error to retry on."""
    HandoffInput.model_validate({"reason": "the draft needs writing"})
    with pytest.raises(ValidationError):
        HandoffInput.model_validate({"reason": "go"})
    with pytest.raises(ValidationError):
        HandoffInput.model_validate({"reason": "long enough reason", "x": 1})


@pytest.mark.unit
def test_user_authored_transfer_to_tools_are_refused() -> None:
    """docs/30 invariant 6: users cannot register their own handoff tools."""
    with pytest.raises(CompileError, match="reserved"):
        check_no_user_handoff_tools(
            ["get_time", "transfer_to_prod"], where="system.yaml"
        )
    check_no_user_handoff_tools(["get_time"], where="system.yaml")  # fine


@pytest.mark.unit
def test_handoff_tool_name_for_end() -> None:
    assert handoff_tool_name("END") == "transfer_to_end"
    assert handoff_tool_name("worker_x") == "transfer_to_worker_x"


# --- HITL payload round-trips -----------------------------------------------------


@pytest.mark.unit
def test_interrupt_payload_round_trip() -> None:
    pending = ApprovalRequired(
        approval_id="send-1",
        prompt="Send it?",
        context={"recipient": "x@example.com", "tool_ref": "local/send@v1"},
    )
    raw = interrupt_payload(pending, agent_name="worker", tool_ref="local/send@v1")
    parsed = parse_payload(raw)
    assert parsed is not None
    assert parsed.approval_id == "send-1"
    assert parsed.agent_name == "worker"
    assert parsed.context["recipient"] == "x@example.com"


@pytest.mark.unit
def test_parse_payload_ignores_foreign_interrupts() -> None:
    assert parse_payload({"unrelated": True}) is None
    assert parse_payload("a string") is None
    assert parse_payload(None) is None


@pytest.mark.unit
def test_resolution_record_round_trip() -> None:
    record = resolution_record("rejected", "too risky")
    resolution = parse_resolution(record)
    assert resolution.decision == "rejected"
    assert resolution.reason == "too risky"
    with pytest.raises(ValidationError):
        parse_resolution({"decision": "maybe"})  # garbage must not pass


# --- ApprovalRequired through the dispatch path ------------------------------------


class _In(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class _Out(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool


async def _gated_handler(inputs: BaseModel, ctx: RunContext) -> _Out:
    if not ctx.approval_resolved("gate-1"):
        raise ApprovalRequired(approval_id="gate-1", prompt="Proceed?")
    if ctx.approval_decision("gate-1") == "rejected":
        return _Out(ok=False)
    return _Out(ok=True)


def _registry() -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            descriptor=ToolDescriptor(
                name="gated", ref="local/gated", version="v1",
                description="approval-gated",
            ),
            input_schema=_In,
            output_schema=_Out,
            handler=_gated_handler,
            timeout_s=5.0,
        )
    )
    return registry


def _ctx(approvals: dict[str, dict[str, Any]] | None = None) -> RunContext:
    return RunContext(
        run_id="R" * 26,
        agent_name="tester",
        session=Session.new(project="unit"),
        tool_ref="local/gated@v1",
        retry_policy=RetryPolicy(initial_delay_s=0.01),
        approvals=approvals or {},
    )


@pytest.mark.unit
async def test_approval_required_passes_through_dispatch_unwrapped() -> None:
    """docs/32: control flow, not error — never wrapped in ToolHandlerError,
    never retried, and no tool.completed failure event."""
    events: list[Any] = []

    def emit(event_cls: type, **fields: Any) -> None:
        events.append((event_cls, fields))

    with pytest.raises(ApprovalRequired) as excinfo:
        await _registry().dispatch(
            "gated", ["gated"], {"text": "x"}, _ctx(), emit=emit
        )
    assert excinfo.value.approval_id == "gate-1"
    completed = [e for e, _ in events if e is ToolCompleted]
    assert completed == []  # the pause is not a tool failure


@pytest.mark.unit
async def test_resolved_approval_lets_the_handler_proceed() -> None:
    approved = await _registry().dispatch(
        "gated",
        ["gated"],
        {"text": "x"},
        _ctx({"gate-1": {"decision": "approved", "reason": None}}),
    )
    assert approved == _Out(ok=True)
    rejected = await _registry().dispatch(
        "gated",
        ["gated"],
        {"text": "x"},
        _ctx({"gate-1": {"decision": "rejected", "reason": "no"}}),
    )
    assert rejected == _Out(ok=False)


@pytest.mark.unit
def test_run_context_approval_accessors() -> None:
    ctx = _ctx({"a-1": {"decision": "rejected", "reason": "risky"}})
    assert ctx.approval_resolved("a-1") is True
    assert ctx.approval_resolved("a-2") is False
    assert ctx.approval_decision("a-1") == "rejected"
    assert ctx.approval_reason("a-1") == "risky"
    assert ctx.approval_reason("a-2") is None
    with pytest.raises(KeyError):
        ctx.approval_decision("a-2")
