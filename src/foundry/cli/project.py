"""`foundry project new <name>` — project skeleton + branch (docs/62).

Creates ``projects/<name>/`` with an ``evals/`` directory and a README,
switches to (creating if needed) the ``foundry/<name>`` branch, and commits
the skeleton there. The meta-agent scaffolds everything else during the
forge bootstrap — ``project new`` deliberately ships NO system.yaml, so
``foundry forge`` detects the bootstrap case from project state.

Exit codes: 0 created, 1 refused (already exists), 2 unexpected failure.
"""

from __future__ import annotations

import re
from pathlib import Path

from foundry.core.errors import ConfigValidationError, FoundryError
from foundry.observability.logging import configure_logging
from foundry.versioning.git_backend import GitBackend

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

_README_TEMPLATE = """\
# {name}

A foundry project. Configure it by hand or let the meta-agent do it:

    foundry forge {name} \\
      --description "what this system should do" \\
      --eval projects/{name}/evals/<eval-set>.yaml \\
      --threshold 0.9 --max-iter 5

Put the eval set under `evals/` FIRST — the forge loop optimises toward
it, and the meta-agent is not allowed to modify it.
"""


def execute_project_new(
    name: str, *, projects_root: Path | None = None
) -> int:
    """The `foundry project new` implementation. Returns the exit code."""
    configure_logging()
    try:
        if not _NAME_RE.match(name):
            raise ConfigValidationError(
                f"invalid project name {name!r}; expected "
                "^[a-z][a-z0-9_-]{0,63}$",
                context={"name": name},
            )
        root = (projects_root or Path.cwd() / "projects").resolve()
        project_dir = root / name
        if project_dir.exists():
            print(
                f"project {name!r} already exists at {project_dir}; "
                "refusing to overwrite."
            )
            return 1
        backend = GitBackend.discover(root if root.is_dir() else root.parent)
        if backend.is_dirty():
            print(
                "working tree has uncommitted changes; commit or stash "
                "before creating a project (the skeleton lands in its own "
                "commit on the new branch)."
            )
            return 1
        branch = f"foundry/{name}"
        backend.ensure_branch(branch)
        (project_dir / "evals").mkdir(parents=True)
        (project_dir / "README.md").write_text(
            _README_TEMPLATE.format(name=name)
        )
        (project_dir / "evals" / ".gitkeep").write_text("")
        commit_sha = backend.commit(
            [
                project_dir / "README.md",
                project_dir / "evals" / ".gitkeep",
            ],
            f"chore({name}): project skeleton (foundry project new)",
        )
        print(f"Created {project_dir} on branch {branch} ({commit_sha[:8]}).")
        print("Next steps:")
        print(f"  1. add an eval set under {project_dir / 'evals'}")
        print(
            f"  2. foundry forge {name} --description \"...\" "
            f"--eval projects/{name}/evals/<set>.yaml"
        )
        return 0
    except FoundryError as exc:
        from foundry.cli._helpers import print_foundry_error

        print_foundry_error(exc)
        return 2


__all__ = ["execute_project_new"]
