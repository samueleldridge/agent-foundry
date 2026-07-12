"""Authentication plug-point (docs/70 § Authentication).

``AuthBackend`` is a Protocol so institutions plug in OIDC/SSO/mTLS
without subclassing foundry code. Built-ins:

- :class:`BearerTokenAuth` — validates ``Authorization: Bearer <token>``
  against a configured token set (constructor arg or the
  ``FOUNDRY_API_TOKENS`` env var, comma-separated). The Phase 8 "bearer
  stub": static token list; JWT-signature validation is the documented
  extension point.
- :class:`NoAuth` — dev only. REFUSES to construct when
  ``FOUNDRY_ENV=prod`` (docs/70 invariant 4).

``/health`` and ``/openapi.json`` (+ ``/docs``) are exempt — operationally
necessary probes (docs/70). The resolved :class:`AuthContext` carries the
operator identity for the audit trail; the run manager records it on the
runs an operator starts/resumes.
"""

from __future__ import annotations

import hmac
import os
from typing import Protocol

from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict

from foundry.core.errors import ConfigError


class AuthContext(BaseModel):
    """Operator identity attached to authenticated requests (docs/70:
    propagates into the audit trail)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str
    scheme: str
    roles: tuple[str, ...] = ()


class AuthBackend(Protocol):
    async def authenticate(self, request: Request) -> AuthContext:
        """Raises HTTPException(401/403) on rejection; returns the
        operator identity on success."""
        ...


class NoAuth:
    """Dev-only pass-through. Refuses to start in prod."""

    def __init__(self) -> None:
        if os.environ.get("FOUNDRY_ENV", "").strip().lower() == "prod":
            raise ConfigError(
                "NoAuth is forbidden when FOUNDRY_ENV=prod (docs/70 "
                "invariant 4) — configure BearerTokenAuth (FOUNDRY_API_TOKENS)"
                " or an institution AuthBackend",
                context={"env": "prod", "auth_backend": "NoAuth"},
            )

    async def authenticate(self, request: Request) -> AuthContext:
        return AuthContext(subject="anonymous", scheme="none")


class BearerTokenAuth:
    """Static bearer-token list. Comparison is constant-time."""

    def __init__(self, tokens: set[str] | None = None) -> None:
        if tokens is None:
            raw = os.environ.get("FOUNDRY_API_TOKENS", "")
            tokens = {t.strip() for t in raw.split(",") if t.strip()}
        if not tokens:
            raise ConfigError(
                "BearerTokenAuth configured with no tokens — set "
                "FOUNDRY_API_TOKENS (comma-separated) or pass tokens= "
                "explicitly",
                context={"auth_backend": "BearerTokenAuth"},
            )
        self._tokens = tokens

    async def authenticate(self, request: Request) -> AuthContext:
        header = request.headers.get("Authorization", "")
        scheme, _, token = header.partition(" ")
        if not header:
            raise HTTPException(401, detail={"error": "authentication required"})
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(401, detail={"error": "invalid token"})
        for candidate in self._tokens:
            if hmac.compare_digest(candidate, token.strip()):
                return AuthContext(subject="bearer-token", scheme="bearer")
        raise HTTPException(401, detail={"error": "invalid token"})


def default_auth_backend() -> AuthBackend:
    """FOUNDRY_API_TOKENS set → bearer auth; otherwise NoAuth (which
    itself refuses under FOUNDRY_ENV=prod, so a prod deploy without
    tokens fails loudly at startup)."""
    if os.environ.get("FOUNDRY_API_TOKENS", "").strip():
        return BearerTokenAuth()
    return NoAuth()


__all__ = [
    "AuthBackend",
    "AuthContext",
    "BearerTokenAuth",
    "NoAuth",
    "default_auth_backend",
]
