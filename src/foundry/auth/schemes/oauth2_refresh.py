"""oauth2_refresh_token scheme: user-delegated flow.

Credentials carry a long-lived ``refresh_token``; the helper rotates the
short-lived access token via the token endpoint when expired
(docs/23 § oauth2_refresh_token).
"""

from __future__ import annotations

import httpx
from pydantic import BaseModel, ConfigDict

from foundry.auth.schemes.oauth2_client_creds import _post_token_request
from foundry.auth.token_cache import TokenCache
from foundry.core.connection import ResolvedConnectionCredentials, SecretValue


class OAuth2RefreshTokenConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token_url: str
    client_id: str
    early_refresh_buffer_s: int = 60


async def fetch_access_token(
    config: OAuth2RefreshTokenConfig,
    credentials: ResolvedConnectionCredentials,
    cache: TokenCache,
    *,
    cache_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SecretValue:
    refresh_token = credentials.require("refresh_token").reveal()

    async def _fetch() -> tuple[str, float | None]:
        form = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": config.client_id,
        }
        return await _post_token_request(config.token_url, form, transport)

    return await cache.get_or_fetch(
        cache_key, _fetch, early_refresh_buffer_s=config.early_refresh_buffer_s
    )


async def build_headers(
    config: OAuth2RefreshTokenConfig,
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


__all__ = ["OAuth2RefreshTokenConfig", "build_headers", "fetch_access_token"]
