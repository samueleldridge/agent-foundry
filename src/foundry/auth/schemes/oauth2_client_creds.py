"""oauth2_client_credentials scheme: server-to-server token flow.

Config carries token_url / scopes / audience; credentials carry client_id +
client_secret. The helper fetches, caches, and early-refreshes the derived
access token via ``TokenCache`` (docs/23 § oauth2_client_credentials).
"""

from __future__ import annotations

import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field

from foundry.auth.token_cache import TokenCache
from foundry.core.connection import ResolvedConnectionCredentials, SecretValue
from foundry.core.errors import ConnectionAuthError


class OAuth2ClientCredentialsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token_url: str
    scopes: list[str] = Field(default_factory=list)
    audience: str | None = None
    grant_type: str = "client_credentials"
    early_refresh_buffer_s: int = 60


async def _post_token_request(
    token_url: str,
    form: dict[str, str],
    transport: httpx.AsyncBaseTransport | None,
) -> tuple[str, float | None]:
    async with httpx.AsyncClient(transport=transport, timeout=30.0) as client:
        response = await client.post(token_url, data=form)
    if response.status_code >= 400:
        raise ConnectionAuthError(
            f"token endpoint {token_url} rejected the request "
            f"(HTTP {response.status_code})",
            context={"token_url": token_url, "http_status": response.status_code},
        )
    payload: dict[str, Any] = response.json()
    token = payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise ConnectionAuthError(
            f"token endpoint {token_url} returned no access_token",
            context={"token_url": token_url,
                     "payload_keys": sorted(payload.keys())},
        )
    expires_in = payload.get("expires_in")
    expires_at = time.time() + float(expires_in) if expires_in else None
    return token, expires_at


async def fetch_access_token(
    config: OAuth2ClientCredentialsConfig,
    credentials: ResolvedConnectionCredentials,
    cache: TokenCache,
    *,
    cache_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SecretValue:
    client_id = credentials.require("client_id").reveal()
    client_secret = credentials.require("client_secret").reveal()

    async def _fetch() -> tuple[str, float | None]:
        form = {
            "grant_type": config.grant_type,
            "client_id": client_id,
            "client_secret": client_secret,
        }
        if config.scopes:
            form["scope"] = " ".join(config.scopes)
        if config.audience:
            form["audience"] = config.audience
        return await _post_token_request(config.token_url, form, transport)

    return await cache.get_or_fetch(
        cache_key, _fetch, early_refresh_buffer_s=config.early_refresh_buffer_s
    )


async def build_headers(
    config: OAuth2ClientCredentialsConfig,
    credentials: ResolvedConnectionCredentials,
    cache: TokenCache,
    *,
    cache_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, str]:
    token = await fetch_access_token(
        config, credentials, cache, cache_key=cache_key, transport=transport
    )
    return {"Authorization": f"Bearer {token.reveal()}"}


__all__ = [
    "OAuth2ClientCredentialsConfig",
    "build_headers",
    "fetch_access_token",
]
