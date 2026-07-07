"""jwt_bearer scheme: JWT-assertion flow, RFC 7523 (docs/23 § jwt_bearer).

Phase 2a supports HS256 (stdlib hmac) out of the box. RS256/ES256 need the
``cryptography`` package, which is not a pinned dependency yet — requesting
them raises a structured error naming the missing dependency rather than
silently mis-signing. (Google/Salesforce-style RS256 lands when a consumer
project actually needs it.)
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

import httpx
from pydantic import BaseModel, ConfigDict, Field

from foundry.auth.schemes.oauth2_client_creds import _post_token_request
from foundry.auth.token_cache import TokenCache
from foundry.core.connection import ResolvedConnectionCredentials, SecretValue
from foundry.core.errors import ConnectionConfigError

_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"


class JWTBearerConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    token_url: str
    issuer: str
    audience: str
    subject: str | None = None
    scopes: list[str] = Field(default_factory=list)
    algorithm: str = "HS256"
    expiry_s: int = Field(default=300, ge=30, le=3600)
    early_refresh_buffer_s: int = 60


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def build_assertion(
    config: JWTBearerConfig,
    credentials: ResolvedConnectionCredentials,
    *,
    now: float | None = None,
) -> str:
    """Construct + sign the JWT assertion. HS256 only in Phase 2a."""
    if config.algorithm != "HS256":
        raise ConnectionConfigError(
            f"jwt_bearer algorithm {config.algorithm!r} requires the "
            "'cryptography' package, which is not a foundry dependency in "
            "Phase 2a; supported now: HS256",
            context={"algorithm": config.algorithm, "supported": ["HS256"]},
        )
    key = credentials.require("private_key").reveal()
    issued_at = int(now if now is not None else time.time())
    header = {"alg": "HS256", "typ": "JWT"}
    claims = {
        "iss": config.issuer,
        "aud": config.audience,
        "iat": issued_at,
        "exp": issued_at + config.expiry_s,
        "jti": str(uuid.uuid4()),
    }
    if config.subject is not None:
        claims["sub"] = config.subject
    if config.scopes:
        claims["scope"] = " ".join(config.scopes)
    signing_input = (
        f"{_b64url(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64url(json.dumps(claims, separators=(',', ':')).encode())}"
    )
    signature = hmac.new(key.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{_b64url(signature)}"


async def fetch_access_token(
    config: JWTBearerConfig,
    credentials: ResolvedConnectionCredentials,
    cache: TokenCache,
    *,
    cache_key: str,
    transport: httpx.AsyncBaseTransport | None = None,
) -> SecretValue:
    async def _fetch() -> tuple[str, float | None]:
        assertion = build_assertion(config, credentials)
        form = {"grant_type": _GRANT_TYPE, "assertion": assertion}
        return await _post_token_request(config.token_url, form, transport)

    return await cache.get_or_fetch(
        cache_key, _fetch, early_refresh_buffer_s=config.early_refresh_buffer_s
    )


__all__ = ["JWTBearerConfig", "build_assertion", "fetch_access_token"]
