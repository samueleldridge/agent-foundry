"""Handler for publish_greeting@v1 — the HITL-gated action (docs/32).

The approval_id is STABLE across re-invocation (run id + input hash), so
after the operator answers, the framework re-runs this handler and the
``ctx.approval_resolved`` check routes to the publish / rejection branch
instead of re-raising.
"""

import hashlib

from schemas import PublishIn, PublishOut

from foundry.core.errors import ApprovalRequired
from foundry.core.tool import RunContext


async def handle(inputs: PublishIn, ctx: RunContext) -> PublishOut:
    stable_hash = hashlib.sha256(inputs.text.encode()).hexdigest()[:8]
    approval_id = f"publish-{ctx.run_id}-{stable_hash}"

    if not ctx.approval_resolved(approval_id):
        raise ApprovalRequired(
            approval_id=approval_id,
            prompt=f"Publish this greeting to the team channel? {inputs.text!r}",
            context={
                "text": inputs.text,
                "tool_ref": ctx.tool_ref,
            },
        )

    if ctx.approval_decision(approval_id) == "rejected":
        reason = ctx.approval_reason(approval_id) or "no reason given"
        return PublishOut(published=False, detail=f"operator rejected: {reason}")

    # Approved — "publish" (the example system has no real channel; a real
    # project would send via a connection here).
    return PublishOut(published=True, detail="greeting published to the team channel")
