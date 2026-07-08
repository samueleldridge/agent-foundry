"""Handler for http_get_json@v2."""

import json
import time

from schemas import HttpGetIn, HttpGetOut

from foundry.core.errors import ConnectionAuthError
from foundry.core.tool import RunContext


async def handle(inputs: HttpGetIn, ctx: RunContext) -> HttpGetOut:
    conn = await ctx.connections.get("service")
    client = conn.client  # httpx.AsyncClient
    started = time.monotonic()
    response = await client.get(inputs.path, params=inputs.query)
    if response.status_code in (401, 403):
        raise ConnectionAuthError(
            f"service returned HTTP {response.status_code} for {inputs.path}",
            context={"status": response.status_code, "path": inputs.path},
        )
    try:
        body = response.json()
    except json.JSONDecodeError:
        body = response.text
    return HttpGetOut(
        status_code=response.status_code,
        json_body=body,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        url=str(response.request.url),
    )
