"""Handler for utc_now@v1."""

from datetime import UTC, datetime

from schemas import UtcNowIn, UtcNowOut

from foundry.core.tool import RunContext


async def handle(inputs: UtcNowIn, ctx: RunContext) -> UtcNowOut:
    now = datetime.now(UTC)
    return UtcNowOut(iso_time=now.isoformat(), unix_ms=int(now.timestamp() * 1000))
