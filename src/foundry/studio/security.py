"""Studio security wiring: bearer auth, redaction, sandbox helpers
(docs/72 § Security posture).

- **Bearer**: optional; when a token is configured every ``/api/*`` route
  requires ``Authorization: Bearer <token>``. SSE consumers may pass the
  token as a ``?token=`` query param (EventSource cannot set headers); the
  value is never logged.
- **Redaction**: every artifact/trajectory/connection payload passes
  through the SAME redactor the span pipeline uses
  (:func:`foundry.observability.redaction.redact_attributes`) before
  serialisation — no secret reaches the browser (contract-tested).
- **Sandbox**: writes resolve through :class:`foundry.security.PathSandbox`
  scoped exactly like the meta-agent's; violations surface as 403 with a
  ``studio.sandbox_refused`` event (mapped in app.py).
"""

from __future__ import annotations

import secrets as _secrets
from typing import Any

from fastapi import HTTPException, Request

from foundry.observability.redaction import redact_attributes
from foundry.versioning.audit import Operator


def studio_operator() -> Operator:
    """The audit-log operator identity for studio-driven mutations."""
    return Operator(kind="studio")


def redacted(value: Any) -> Any:
    """Default-deny redaction for any JSON-shaped payload the studio is
    about to serialise to the browser. Dicts go through the span-attribute
    redactor (denylist keys + secret-shaped values dropped, previews
    truncated); lists recurse; scalars pass through unless secret-shaped."""
    from foundry.observability.redaction import value_looks_secret

    if isinstance(value, dict):
        return redact_attributes(value)
    if isinstance(value, list):
        return [redacted(item) for item in value]
    if isinstance(value, str) and value_looks_secret(value):
        return "[REDACTED]"
    return value


def make_auth_dependency(token: str | None) -> Any:
    """FastAPI dependency enforcing the optional bearer token on /api/*."""

    async def _auth(request: Request) -> None:
        if token is None:
            return
        supplied = ""
        header = request.headers.get("Authorization", "")
        if header.startswith("Bearer "):
            supplied = header.removeprefix("Bearer ").strip()
        if not supplied:
            # EventSource fallback: token via query param, never logged.
            supplied = request.query_params.get("token", "")
        if not supplied or not _secrets.compare_digest(supplied, token):
            raise HTTPException(
                status_code=401,
                detail={"error": "authentication required"},
            )

    return _auth


__all__ = ["make_auth_dependency", "redacted", "studio_operator"]
