"""Factory for http_service@v1: httpx.AsyncClient with API-key header."""

import time

import httpx

from foundry.auth.schemes.api_key import APIKeyConfig, build_headers
from foundry.core.connection import (
    ConnectionContext,
    ConnectionHealth,
    ResolvedConnectionCredentials,
)


class HTTPServiceConnection:
    """Connection protocol impl: authenticated httpx client + health probe."""

    def __init__(self, ref: str, client: httpx.AsyncClient, health_path: str) -> None:
        self.ref = ref
        self.slot = ""
        self._client = client
        self._health_path = health_path

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def health(self) -> ConnectionHealth:
        from datetime import UTC, datetime

        started = time.monotonic()
        try:
            response = await self._client.get(self._health_path)
        except httpx.HTTPError as exc:
            return ConnectionHealth(
                ok=False,
                message=f"transport error: {exc}",
                checked_at=datetime.now(UTC),
            )
        return ConnectionHealth(
            ok=response.status_code < 400,
            latency_ms=int((time.monotonic() - started) * 1000),
            message=f"GET {self._health_path} -> {response.status_code}",
            checked_at=datetime.now(UTC),
        )

    async def close(self) -> None:
        await self._client.aclose()


async def build_connection(
    config,  # HTTPServiceConfig instance (validated by the registry)
    credentials: ResolvedConnectionCredentials,
    ctx: ConnectionContext,
) -> HTTPServiceConnection:
    headers = {}
    if credentials.fields:
        headers = build_headers(
            APIKeyConfig(
                header_name=config.api_key_header,
                value_template=config.api_key_template,
            ),
            credentials,
        )
    client = httpx.AsyncClient(
        base_url=config.base_url,
        headers=headers,
        timeout=config.timeout_s,
        transport=ctx.http_transport,
    )
    return HTTPServiceConnection("catalog/http_service@v1", client, config.health_path)
