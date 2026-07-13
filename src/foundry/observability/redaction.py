"""Field-level redaction for exported observability data (docs/80 § Privacy).

Two rules, applied to every attribute mapping that leaves the process
(OTel span attributes, SQLite mirror rows never carry free-form config, so
spans are the main consumer):

1. **Default-deny denylist** — any key matching the shared
   ``foundry.auth.redactor.DENYLIST_KEY_RE`` (``api_key`` / ``password`` /
   ``secret`` / ``token`` / ``private_key`` / ``credential``) is dropped,
   and any *string value* matching a known secret shape (AWS key id,
   ``sk-ant-``, ``sk-``, PEM header) is dropped regardless of its key.
2. **Preview truncation** — free-text preview fields are truncated to
   ``PREVIEW_MAX_CHARS`` so a single attribute can't smuggle a payload past
   span-size limits (docs/80 § Failure modes).
"""

from __future__ import annotations

from typing import Any

from foundry.auth.redactor import DENYLIST_KEY_RE, SECRET_VALUE_PATTERNS

PREVIEW_MAX_CHARS = 500

_PREVIEW_KEYS = frozenset({"input_preview", "output_preview", "message", "prompt"})


def _key_denied(key: str) -> bool:
    """The shared denylist matches ``token`` — which would also swallow the
    token-COUNT fields every LLM event carries (``input_tokens``,
    ``saved_tokens_estimate``, ...). Counts are not secrets: strip the
    plural ``tokens`` before matching so ``token``/``api_token`` still
    drop but ``*_tokens`` counters survive."""
    return DENYLIST_KEY_RE.search(key.replace("tokens", "")) is not None


def value_looks_secret(value: Any) -> bool:
    """True when a *value* (regardless of key name) matches a known secret
    shape. Key-name matching is handled separately by the denylist."""
    return isinstance(value, str) and any(p.search(value) for p in SECRET_VALUE_PATTERNS)


def truncate_preview(text: str, limit: int = PREVIEW_MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "…[truncated]"


def redact_attributes(attributes: dict[str, Any]) -> dict[str, Any]:
    """Drop denylisted keys + secret-shaped values; truncate previews.

    Returns a new dict; never mutates the input. Nested dicts are redacted
    recursively (span attributes are flat, but event payloads may nest)."""
    out: dict[str, Any] = {}
    for key, value in attributes.items():
        if _key_denied(key):
            continue
        if value_looks_secret(value):
            continue
        if isinstance(value, dict):
            out[key] = redact_attributes(value)
            continue
        if isinstance(value, str) and key in _PREVIEW_KEYS:
            value = truncate_preview(value)
        out[key] = value
    return out


__all__ = [
    "PREVIEW_MAX_CHARS",
    "redact_attributes",
    "truncate_preview",
    "value_looks_secret",
]
