"""Per-scheme auth helpers (docs/23 § Auth schemes).

A scheme knows how to produce an authenticated header / signed request /
TLS context / bearer token from typed inputs. It does NOT know about the
target system — that is the connection factory's concern.
"""

from __future__ import annotations

from foundry.auth.schemes import (
    api_key,
    basic_auth,
    custom,
    jwt_bearer,
    mtls,
    oauth2_client_creds,
    oauth2_refresh,
    sigv4,
)

__all__ = [
    "api_key",
    "basic_auth",
    "custom",
    "jwt_bearer",
    "mtls",
    "oauth2_client_creds",
    "oauth2_refresh",
    "sigv4",
]
