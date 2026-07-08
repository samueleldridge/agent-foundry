"""normalize_input — deterministic pre-agent turn cleanup."""

from typing import Any

from foundry.core import RunContext


async def normalize(state_view: dict[str, Any], ctx: RunContext) -> dict[str, Any]:
    raw = state_view.get("raw_turns") or []
    turns = [turn.strip() for turn in raw if isinstance(turn, str) and turn.strip()]
    return {"turns": turns}
