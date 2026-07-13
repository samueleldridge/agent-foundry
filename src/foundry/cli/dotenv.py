"""Local `.env` loading for the `foundry` CLI (local-testing convenience).

This is a **CLI-layer** concern only. `foundry.config` and the rest of the
library never read files from the working directory — they resolve secrets
from `os.environ` via `EnvSecretsProvider` (docs/12). Auto-reading a `.env`
belongs at the console-script entry point so that importing foundry as a
library, or running it under an ASGI server, never surprise-reads the disk.

Behaviour (mirrors the well-worn dotenv convention):

- The real process environment ALWAYS wins. A key already present in
  `os.environ` is never overwritten by the file — so `export FOO=... ; foundry
  ...` and CI-injected secrets take precedence over a stale `.env`.
- Opt out with `FOUNDRY_NO_ENV_FILE=1`.
- Point at an explicit file with `FOUNDRY_ENV_FILE=/path/to/file`; otherwise the
  nearest `.env` walking up from the current directory is used.
- Malformed lines are skipped, never fatal. No variable expansion, no shell
  evaluation — values are taken literally (quotes stripped).

No third-party dependency: the parser below is deliberately small. `.env` is
gitignored, so keys placed there never reach the repo (CLAUDE.md no-secrets
invariant).
"""

from __future__ import annotations

import os
from pathlib import Path

__all__ = ["find_env_file", "load_local_env", "parse_env_text"]

_MAX_PARENTS = 25


def find_env_file() -> Path | None:
    """Locate the `.env` to load, honouring the opt-out + explicit-path env
    vars. Returns None when loading is disabled or no file is found."""
    if os.environ.get("FOUNDRY_NO_ENV_FILE", "").strip():
        return None
    explicit = os.environ.get("FOUNDRY_ENV_FILE", "").strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        return candidate if candidate.is_file() else None
    here = Path.cwd()
    for directory in (here, *here.parents[:_MAX_PARENTS]):
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def parse_env_text(text: str) -> list[tuple[str, str]]:
    """Parse `.env` content into ordered (key, value) pairs. Skips blanks,
    comments, and malformed lines. Strips an optional leading ``export`` and
    a single layer of matching surrounding quotes; no expansion."""
    pairs: list[tuple[str, str]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export ") or line.startswith("export\t"):
            line = line[len("export"):].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key or not _is_valid_key(key):
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        pairs.append((key, value))
    return pairs


def _is_valid_key(key: str) -> bool:
    first = key[0]
    if not (first.isalpha() or first == "_"):
        return False
    return all(ch.isalnum() or ch == "_" for ch in key)


def load_local_env() -> list[str]:
    """Load the nearest `.env` into `os.environ` without overriding vars that
    are already set. Returns the names actually applied (for reporting).
    Silent + best-effort: unreadable files yield an empty list."""
    path = find_env_file()
    if path is None:
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    applied: list[str] = []
    for key, value in parse_env_text(text):
        if key in os.environ:
            continue
        os.environ[key] = value
        applied.append(key)
    return applied
