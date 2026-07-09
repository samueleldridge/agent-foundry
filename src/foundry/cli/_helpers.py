"""Shared helpers for the Phase 5 CLI executors (rollback / versions /
diff / catalog promote)."""

from __future__ import annotations

import sys
from pathlib import Path

from foundry.core.errors import ConfigLoadError, FoundryError


def resolve_project_dir(project: str) -> Path:
    """Accept a project PATH (projects/hello) or a bare NAME (hello,
    resolved against ./projects/ from the cwd)."""
    candidates = [Path(project), Path("projects") / project]
    for candidate in candidates:
        if (candidate / "system.yaml").is_file():
            return candidate.resolve()
    raise ConfigLoadError(
        f"project {project!r} not found; checked "
        f"{', '.join(str(c) for c in candidates)} (need a directory "
        "containing system.yaml)",
        context={"project": project,
                 "checked": [str(c) for c in candidates]},
    )


def print_foundry_error(exc: FoundryError) -> None:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    for key, value in exc.context.items():
        if value is None or f"{key}:" in str(exc):
            continue
        print(f"  {key}: {value}", file=sys.stderr)


__all__ = ["print_foundry_error", "resolve_project_dir"]
