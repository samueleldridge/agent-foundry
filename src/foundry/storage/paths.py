"""Standard filesystem paths.

``FOUNDRY_HOME`` overrides the default ``~/.foundry`` root (used heavily by
tests to keep artifacts inside tmp_path). Full storage layout lands in
Phase 9; Phase 1 needs only the per-run artifact directory.
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


__all__ = ["foundry_home", "run_dir", "runs_root"]
