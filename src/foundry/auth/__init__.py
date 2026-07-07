"""Authentication: scheme helpers, token cache, redactor (docs/23).

``AuthScheme`` itself lives in ``foundry.core.connection``; this package
holds the concrete helpers each connection's auth.py composes with.
"""

from __future__ import annotations

from foundry.auth import schemes
from foundry.auth.redactor import looks_secret, redact_config
from foundry.auth.token_cache import CachedToken, TokenCache
from foundry.core.connection import AuthScheme

__all__ = [
    "AuthScheme",
    "CachedToken",
    "TokenCache",
    "looks_secret",
    "redact_config",
    "schemes",
]
