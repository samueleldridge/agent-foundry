"""Standard filesystem paths (docs/81 § The full filesystem layout).

``FOUNDRY_HOME`` overrides the default ``~/.foundry`` root (used heavily by
tests to keep artifacts inside tmp_path). Every resolver reads the env var at
call time so a test's ``monkeypatch.setenv`` is honoured.
"""

from __future__ import annotations

import os
from pathlib import Path


def foundry_home() -> Path:
    override = os.environ.get("FOUNDRY_HOME")
    return Path(override) if override else Path.home() / ".foundry"


def runs_root() -> Path:
    return foundry_home() / "runs"


def run_dir(run_id: str) -> Path:
    return runs_root() / run_id


def archives_root() -> Path:
    """Monthly tarballs of aged-out artifacts (docs/81 § Archival pattern)."""
    return foundry_home() / "archives"


def pinned_global_path() -> Path:
    """Global pin list; one ``<kind> <id> [# reason]`` per line."""
    return foundry_home() / "pinned_global.txt"


def observability_db_path() -> Path:
    """SQLite event mirror (docs/80); owned by foundry.observability."""
    return foundry_home() / "observability.db"


__all__ = [
    "archives_root",
    "foundry_home",
    "observability_db_path",
    "pinned_global_path",
    "run_dir",
    "runs_root",
]
