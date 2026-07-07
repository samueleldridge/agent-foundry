"""ConnectionDescriptor builder + config hashing (docs/23 § Observability).

The descriptor is the ONLY connection metadata that ever reaches logs,
traces, or run artifacts. ``redacted_config`` is allowlist-projected via
``foundry.auth.redactor`` — anything the ConnectionSpec doesn't explicitly
mark non-sensitive is dropped.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from foundry.auth.redactor import redact_config
from foundry.core.connection import AuthScheme, ConnectionDescriptor


def config_hash(config: dict[str, Any]) -> str:
    """Short, stable hash of the resolved (secret-free) config. Part of the
    pool key: same ref + same config → same pool entry."""
    canonical = json.dumps(config, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def build_descriptor(
    *,
    ref: str,
    auth_scheme: AuthScheme,
    config: dict[str, Any],
    non_sensitive_config_fields: list[str],
    slot: str = "",
    principal: str | None = None,
) -> ConnectionDescriptor:
    return ConnectionDescriptor(
        ref=ref,
        slot=slot,
        auth_scheme=auth_scheme,
        config_hash=config_hash(config),
        principal=principal,
        redacted_config=redact_config(config, non_sensitive_config_fields),
    )


__all__ = ["build_descriptor", "config_hash"]
