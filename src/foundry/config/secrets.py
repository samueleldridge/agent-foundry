"""Secrets: the SecretsProvider interface + the secret-literal scan.

Secrets never appear in YAML — period (docs/12 § Secrets). The scan runs over
every scalar before Pydantic validation; it is deliberately false-positive
prone (noisy is safer than silent). Users silence a false positive with a
``# foundry:allow-literal`` comment on the offending line.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from foundry.core import CredentialsRef, ResolvedCredentials
from foundry.core.errors import ConfigLoadError

_ALLOW_PRAGMA = "foundry:allow-literal"

# (pattern, label). Order matters: more specific first.
_VALUE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key id"),
    (re.compile(r"sk-ant-[A-Za-z0-9_-]{8,}"), "Anthropic API key"),
    (re.compile(r"sk-[A-Za-z0-9_-]{16,}"), "OpenAI-style API key"),
]

_SENSITIVE_KEY = re.compile(r"(password|secret|token|api_key|apikey)", re.IGNORECASE)
_MIN_SENSITIVE_VALUE_LEN = 8


def _line_allows_literal(raw_lines: list[str], line: int | None) -> bool:
    if line is None or not (1 <= line <= len(raw_lines)):
        return False
    return _ALLOW_PRAGMA in raw_lines[line - 1]


def scan_for_secret_literals(
    data: Any,
    file: Path,
    raw_text: str,
    positions: dict[str, tuple[int, int]] | None = None,
) -> None:
    """Walk every scalar; raise ConfigLoadError on a likely secret literal.

    The error names the pointer and file but NEVER echoes the value.
    """
    raw_lines = raw_text.splitlines()
    positions = positions or {}

    def _fail(pointer: str, reason: str) -> None:
        line_col = positions.get(pointer)
        line = line_col[0] if line_col else None
        if _line_allows_literal(raw_lines, line):
            return
        location = f"{file}" + (f":{line}" if line else "")
        raise ConfigLoadError(
            f"Detected likely secret literal at {pointer} ({location}): {reason}. "
            "Secrets must live in env vars or a secrets provider; use "
            "credentials_ref. If this is a false positive, add a "
            f"'# {_ALLOW_PRAGMA}' comment on that line.",
            context={
                "file": str(file),
                "pointer": pointer,
                "line": line,
                "reason": reason,
            },
        )

    def _walk(node: Any, pointer: str, key: str | None) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                _walk(v, f"{pointer}/{k}", str(k))
        elif isinstance(node, list):
            for i, v in enumerate(node):
                _walk(v, f"{pointer}/{i}", key)
        elif isinstance(node, str):
            for pattern, label in _VALUE_PATTERNS:
                if pattern.search(node):
                    _fail(pointer, f"value matches {label} pattern")
                    return
            if (
                key is not None
                and _SENSITIVE_KEY.search(key)
                and len(node) > _MIN_SENSITIVE_VALUE_LEN
                and not _looks_like_ref(node)
            ):
                _fail(pointer, f"key name {key!r} looks credential-like")

    _walk(data, "", None)


def _looks_like_ref(value: str) -> bool:
    """UPPER_SNAKE env-var names and ${ENV:...} placeholders are references,
    not literals — the common legitimate values under *_key/token keys."""
    return bool(re.fullmatch(r"[A-Z][A-Z0-9_]*", value)) or value.startswith("${ENV:")


# --- SecretsProvider -----------------------------------------------------------


@runtime_checkable
class SecretsProvider(Protocol):
    def resolve(self, ref: CredentialsRef | None) -> ResolvedCredentials: ...


class EnvSecretsProvider:
    """Default provider: resolves kind='env' against os.environ.

    kind='default' returns an empty credential, meaning 'let the SDK /
    transport default chain handle it'. aws_profile / secret_manager land
    with their consumers in later phases.
    """

    def resolve(self, ref: CredentialsRef | None) -> ResolvedCredentials:
        if ref is None or ref.kind == "default":
            return ResolvedCredentials(kind="default", secret=None)
        if ref.kind == "env":
            name = ref.value or ""
            if not name:
                raise ConfigLoadError(
                    "credentials_ref kind='env' requires a value naming the env var",
                    context={"kind": "env"},
                )
            value = os.environ.get(name)
            # Set-but-empty (e.g. `OPENAI_API_KEY=`) is treated exactly like
            # unset: an empty/whitespace secret would otherwise flow into an
            # empty `Authorization: Bearer` header downstream.
            if value is None or not value.strip():
                raise ConfigLoadError(
                    f"environment variable {name!r} is not set or is empty "
                    "(required by a credentials_ref)",
                    context={"env_var": name},
                )
            return ResolvedCredentials(kind="env", secret=value)
        raise ConfigLoadError(
            f"credentials_ref kind {ref.kind!r} is not supported in Phase 1 "
            "(env and default only)",
            context={"kind": ref.kind},
        )


__all__ = [
    "EnvSecretsProvider",
    "SecretsProvider",
    "scan_for_secret_literals",
]
