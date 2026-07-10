"""HITL pause/resume semantics — the langgraph-free half (docs/32).

``ApprovalRequired`` (in ``foundry.core.errors``) is control flow: a tool
raises it, the runtime adapter converts it into a LangGraph interrupt at
the node boundary (the ONLY place langgraph is touched), the checkpointer
persists the pending state durably, and the run pauses with status
``approval_pending``. This module owns the interrupt PAYLOAD shape, the
resolution record threaded back into ``RunContext.approvals``, and the
pending-approval surface the CLI reads.

Lifecycle (docs/32 § Lifecycle):

- pause: tool raises → adapter emits ``approval.required`` → LangGraph
  interrupt persists ``InterruptPayload`` in the checkpoint → run returns
  ``status="approval_pending"``.
- resume: ``foundry resume <run_id> --approve|--reject`` → the runtime
  emits ``approval.resolved`` and re-invokes the paused node with the
  resolution; the handler re-runs, sees ``ctx.approval_resolved(id)`` and
  proceeds (approved) or returns a rejection result (rejected). Event
  sequence numbers continue from the last persisted sequence.

Re-execution warning (docs/32 § Re-execution semantics): the WHOLE paused
node re-runs on resume — sibling tool calls dispatched in the same round
as the approval-raising tool execute again. Handlers must be idempotent;
a handler that re-raises the SAME approval_id after resolution is a
non-idempotent flow bug surfaced as ``OrchestrationError``.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from foundry.core.errors import ApprovalRequired

Decision = Literal["approved", "rejected"]

RUN_STATUS_APPROVAL_PENDING = "approval_pending"


class InterruptPayload(BaseModel):
    """What the checkpointer persists for a pending approval — also the
    shape ``RunResult.pending_approval`` and the CLI surface read."""

    model_config = ConfigDict(extra="forbid")

    approval_id: str
    prompt: str
    context: dict[str, Any] = Field(default_factory=dict)
    agent_name: str
    tool_ref: str | None = None


class ApprovalResolution(BaseModel):
    """The operator's answer, threaded into ``RunContext.approvals``."""

    model_config = ConfigDict(extra="forbid")

    decision: Decision
    reason: str | None = None


def interrupt_payload(
    pending: ApprovalRequired,
    *,
    agent_name: str,
    tool_ref: str | None = None,
) -> dict[str, Any]:
    """Serialise an ``ApprovalRequired`` into the checkpointed payload."""
    return InterruptPayload(
        approval_id=pending.approval_id,
        prompt=pending.prompt,
        context=pending.approval_context,
        agent_name=agent_name,
        tool_ref=tool_ref,
    ).model_dump(mode="json")


def parse_payload(raw: Any) -> InterruptPayload | None:
    """Best-effort parse of a checkpointed interrupt value; None when the
    value is not a foundry approval payload."""
    if not isinstance(raw, dict) or "approval_id" not in raw:
        return None
    try:
        return InterruptPayload.model_validate(raw)
    except Exception:
        return None


def resolution_record(
    decision: Decision, reason: str | None = None
) -> dict[str, Any]:
    """The resume value handed to the paused node (and merged into the
    run's ``approvals`` state channel)."""
    return ApprovalResolution(
        decision=decision, reason=reason
    ).model_dump(mode="json")


def parse_resolution(raw: Any) -> ApprovalResolution:
    """Parse a resume value back into a typed resolution. A malformed
    value fails loudly — a garbage resume must not look like a rejection."""
    return ApprovalResolution.model_validate(raw)


__all__ = [
    "RUN_STATUS_APPROVAL_PENDING",
    "ApprovalResolution",
    "Decision",
    "InterruptPayload",
    "interrupt_payload",
    "parse_payload",
    "parse_resolution",
    "resolution_record",
]
