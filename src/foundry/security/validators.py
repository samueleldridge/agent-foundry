"""Input/output validators (docs/83 § Layered controls).

The heavy lifting is already structural — tool dispatch Pydantic-validates
inputs and outputs against the tool's schemas, configs pass the
secret-literal scan at load, and observability redacts at export. This
module adds the two checks that sit at *content* boundaries rather than
schema boundaries:

- :func:`ensure_no_secret_leak` — refuse to emit text that contains a
  secret-shaped value (the docs/80 contract-test guarantee, callable by
  any surface that is about to hand content to an external channel).
- :func:`validated_json` — parse-and-validate untrusted JSON text against
  a Pydantic model with a structured error instead of a raw traceback.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, ValidationError

from foundry.auth.redactor import SECRET_VALUE_PATTERNS
from foundry.core.errors import SecurityError


def find_secret_shaped_content(text: str) -> list[str]:
    """Pattern NAMES (never the matched values) of secret-shaped content
    found in ``text``. Empty list == clean."""
    findings: list[str] = []
    for pattern in SECRET_VALUE_PATTERNS:
        if pattern.search(text):
            findings.append(pattern.pattern)
    return findings


def ensure_no_secret_leak(text: str, *, where: str) -> str:
    """Raise :class:`SecurityError` (naming the pattern, NEVER the value)
    when ``text`` contains secret-shaped content; return ``text`` unchanged
    otherwise."""
    findings = find_secret_shaped_content(text)
    if findings:
        raise SecurityError(
            f"refusing to emit secret-shaped content at {where} "
            f"(matched {len(findings)} known secret pattern(s))",
            context={"where": where, "patterns": findings},
        )
    return text


def validated_json[ModelT: BaseModel](
    text: str, model: type[ModelT], *, where: str
) -> ModelT:
    """Parse untrusted JSON text and validate it against ``model``.
    Malformed JSON or a schema mismatch raises :class:`SecurityError` with
    a structured summary (no raw payload echo beyond pydantic's own field
    pointers)."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SecurityError(
            f"invalid JSON at {where}: {exc.msg} (line {exc.lineno})",
            context={"where": where},
        ) from exc
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise SecurityError(
            f"schema validation failed at {where}: "
            f"{first['msg']} (field: {'.'.join(str(p) for p in first['loc'])})",
            context={"where": where, "error_count": exc.error_count()},
        ) from exc


__all__ = [
    "ensure_no_secret_leak",
    "find_secret_shaped_content",
    "validated_json",
]
