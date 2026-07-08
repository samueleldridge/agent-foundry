"""Factory for cohere_rerank@v1: httpx client with Cohere bearer auth."""

import time

import httpx

from foundry.auth.schemes.api_key import APIKeyConfig, build_headers
from foundry.core.connection import (
    ConnectionContext,
    ConnectionHealth,
    ResolvedConnectionCredentials,
)


class CohereRerankConnection:
    def __init__(self, ref: str, client: httpx.AsyncClient, model: str) -> None:
        self.ref = ref
        self.slot = ""
        self._client = client
        self.model = model

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    async def health(self) -> ConnectionHealth:
        from datetime import UTC, datetime

        started = time.monotonic()
        try:
            response = await self._client.get("/v1/models")
        except httpx.HTTPError as exc:
            return ConnectionHealth(
                ok=False,
                message=f"transport error: {exc}",
                checked_at=datetime.now(UTC),
            )
        return ConnectionHealth(
            ok=response.status_code < 400,
            latency_ms=int((time.monotonic() - started) * 1000),
            message=f"GET /v1/models -> {response.status_code}",
            checked_at=datetime.now(UTC),
        )

    async def close(self) -> None:
        await self._client.aclose()


async def build_connection(
    config,  # CohereRerankConfig instance
    credentials: ResolvedConnectionCredentials,
    ctx: ConnectionContext,
) -> CohereRerankConnection:
    headers = build_headers(APIKeyConfig(), credentials)
    client = httpx.AsyncClient(
        base_url=config.base_url,
        headers=headers,
        timeout=config.timeout_s,
        transport=ctx.http_transport,
    )
    return CohereRerankConnection("catalog/cohere_rerank@v1", client, config.model)
