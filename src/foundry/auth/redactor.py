"""Credential redaction for descriptors, logs, and traces.

Opt-in allowlist semantics (docs/23 § Attribute redaction): only fields the
ConnectionSpec names in ``non_sensitive_config_fields`` survive into
``ConnectionDescriptor.redacted_config`` — and even a listed field is
dropped (with a warning) if its key or value looks secret-ish. Everything
not listed is dropped, not included-by-default.
"""

from __future__ import annotations

import logging
import re
from typing import Any

_logger = logging.getLogger("foundry.auth.redactor")

DENYLIST_KEY_RE = re.compile(
    r"(password|secret|token|api_key|apikey|private_key|credential)",
    re.IGNORECASE,
)

SECRET_VALUE_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"AKIA[0-9A-Z]{16}"),          # AWS access key id
    re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"),  # Anthropic API key
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),     # OpenAI-style API key
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),  # PEM private key
)


def looks_secret(key: str, value: Any) -> bool:
    if DENYLIST_KEY_RE.search(key):
        return True
    if isinstance(value, str):
        return any(p.search(value) for p in SECRET_VALUE_PATTERNS)
    return False


def redact_config(
    config: dict[str, Any], non_sensitive_fields: list[str]
) -> dict[str, Any]:
    """Project a config dict down to its provably-safe subset."""
    allowed = set(non_sensitive_fields)
    out: dict[str, Any] = {}
    for key, value in config.items():
        if key not in allowed:
            continue
        if looks_secret(key, value):
            _logger.warning(
                "config field %r is allowlisted as non-sensitive but looks "
                "secret-ish; dropping it from the descriptor anyway",
                key,
            )
            continue
        out[key] = value
    return out


__all__ = [
    "DENYLIST_KEY_RE",
    "SECRET_VALUE_PATTERNS",
    "looks_secret",
    "redact_config",
]
