"""Compile-time handoff-tool generation for the supervisor pattern
(docs/30 § Handoff tool generation, docs/31 § Cross-agent communication).

The compiler synthesises one typed ``transfer_to_<worker>`` tool per
target in ``allowed_handoffs[<supervisor>]`` (plus ``transfer_to_end``
when END is allowed). Handoff tools are NOT in the agent's ``tools``
allowlist and never touch the ToolRegistry — they are routing primitives
the agent-step runtime intercepts: calling one records the handoff and
routes the next node; the "output" the LLM sees is just confirmation.

Handoffs are routing hints, not data passes: the worker reads its declared
``read`` fields from merged state, never the supervisor's reasoning text
(docs/31). Users cannot register their own handoff tools (docs/30
invariant 6) — the ``transfer_to_`` prefix is reserved and checked at
compile time.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from foundry.core.errors import CompileError
from foundry.orchestration.patterns import END_SENTINEL, SupervisorPlan

HANDOFF_TOOL_PREFIX = "transfer_to_"
END_HANDOFF_TOOL = "transfer_to_end"


class HandoffInput(BaseModel):
    """The docs/30 handoff-tool input shape."""

    model_config = ConfigDict(extra="forbid")

    reason: str = Field(
        min_length=10,
        description="Why this worker is being invoked.",
    )


class HandoffOutput(BaseModel):
    """Always-true confirmation; the LLM uses it as an acknowledgement."""

    model_config = ConfigDict(extra="forbid")

    handoff_recorded: bool = True


@dataclass(frozen=True)
class HandoffTool:
    """One synthesised handoff tool."""

    name: str
    """``transfer_to_<worker>`` / ``transfer_to_end``."""
    target: str
    """The routing target: a worker name, or END_SENTINEL."""
    description: str


def handoff_tool_name(target: str) -> str:
    if target == END_SENTINEL:
        return END_HANDOFF_TOOL
    return f"{HANDOFF_TOOL_PREFIX}{target}"


def build_handoff_tools(
    plan: SupervisorPlan,
    descriptions: dict[str, str],
) -> tuple[HandoffTool, ...]:
    """The supervisor's handoff tool set, one per allowed target.

    ``descriptions`` maps worker target names to their agent/function
    ``description`` fields (nested sub-flows get a generated line)."""
    tools: list[HandoffTool] = []
    for target in plan.supervisor_targets:
        if target == END_SENTINEL:
            tools.append(
                HandoffTool(
                    name=END_HANDOFF_TOOL,
                    target=END_SENTINEL,
                    description=(
                        "Finish the run: no further workers are needed. "
                        "After this tool confirms, produce your final "
                        "structured answer."
                    ),
                )
            )
            continue
        detail = descriptions.get(target, "")
        tools.append(
            HandoffTool(
                name=handoff_tool_name(target),
                target=target,
                description=(
                    f"Hand off the current task to {target}."
                    + (f" {detail}" if detail else "")
                ),
            )
        )
    return tuple(tools)


def check_no_user_handoff_tools(
    tool_names: list[str], *, where: str
) -> None:
    """docs/30 invariant 6: users cannot register their own handoff tools.
    Any system.yaml tool binding whose logical name uses the reserved
    ``transfer_to_`` prefix fails compile."""
    reserved = sorted(
        name for name in tool_names if name.startswith(HANDOFF_TOOL_PREFIX)
    )
    if reserved:
        raise CompileError(
            f"tool name(s) {', '.join(reserved)} use the reserved "
            f"'{HANDOFF_TOOL_PREFIX}' prefix; handoff tools are "
            "compile-generated, never user-authored (docs/30 invariant 6)",
            context={"file": where, "pointer": "/tools", "names": reserved},
        )


__all__ = [
    "END_HANDOFF_TOOL",
    "HANDOFF_TOOL_PREFIX",
    "HandoffInput",
    "HandoffOutput",
    "HandoffTool",
    "build_handoff_tools",
    "check_no_user_handoff_tools",
    "handoff_tool_name",
]
