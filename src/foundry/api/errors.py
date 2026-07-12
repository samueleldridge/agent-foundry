"""FoundryError → HTTP status mapping (docs/70 § Failure modes).

The API never returns a stack trace: every error body is a structured
``FoundryError.to_dict()`` (or the same shape synthesised for request-
validation failures). Status selection walks the error's class hierarchy
so subclasses inherit their family's status.
"""

from __future__ import annotations

from typing import Any

from foundry.core.errors import (
    CheckpointError,
    ConfigError,
    FoundryError,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimitError,
    RunCancelled,
)
from foundry.core.errors import (
    ConnectionError as FoundryConnectionError,
)

_STATUS_BY_CLASS: list[tuple[type[FoundryError], int]] = [
    # Order matters: first match wins, most specific first.
    (ProviderAuthError, 502),
    (ProviderRateLimitError, 503),
    (ProviderError, 502),
    (FoundryConnectionError, 503),
    (CheckpointError, 503),
    (RunCancelled, 499),
    (ConfigError, 400),
]

_RETRY_AFTER_STATUSES = frozenset({503})


def status_for(exc: FoundryError) -> int:
    for cls, status in _STATUS_BY_CLASS:
        if isinstance(exc, cls):
            return status
    return 500


def status_for_error_class(error_class: str) -> int:
    """Status from a serialised error's class NAME (a completed run's
    ``error.error_class``, where the exception object is gone)."""
    if error_class in ("ProviderAuthError",):
        return 502
    if error_class.startswith("Provider"):
        return 502
    if error_class.startswith(("Connection", "Checkpoint")):
        return 503
    if error_class == "RunCancelled":
        return 499
    return 500


def headers_for(status: int) -> dict[str, str]:
    if status in _RETRY_AFTER_STATUSES:
        return {"Retry-After": "5"}
    return {}


def error_body(exc: FoundryError) -> dict[str, Any]:
    return exc.to_dict()


def validation_error_body(errors: list[dict[str, Any]]) -> dict[str, Any]:
    """Pydantic request-validation failures, reshaped to the structured
    error contract (docs/70: 400 names the failing field)."""
    first = errors[0] if errors else {}
    loc = [str(part) for part in first.get("loc", ()) if part != "body"]
    return {
        "error_class": "ConfigValidationError",
        "message": (
            f"invalid request body: {first.get('msg', 'validation failed')}"
            + (f" (field: {'.'.join(loc)})" if loc else "")
        ),
        "context": {
            "field": ".".join(loc) or None,
            "reason": first.get("msg"),
            "errors": [
                {
                    "field": ".".join(
                        str(p) for p in e.get("loc", ()) if p != "body"
                    ),
                    "reason": e.get("msg"),
                }
                for e in errors
            ],
        },
    }


__all__ = [
    "error_body",
    "headers_for",
    "status_for",
    "status_for_error_class",
    "validation_error_body",
]
