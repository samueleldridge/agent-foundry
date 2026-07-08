"""format_output — deterministic post-agent reply formatting."""

from typing import Any

from foundry.core import RunContext


async def format_reply(state_view: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
    reply = state_view.get("reply") or ""
    return {"formatted_reply": f"[memory_hello] {reply}"}
