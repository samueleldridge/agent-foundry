"""`foundry project new <name>` — project skeleton + branch (docs/62).

Creates ``projects/<name>/`` with an ``evals/`` directory and a README,
switches to (creating if needed) the ``foundry/<name>`` branch, and commits
the skeleton there. The meta-agent scaffolds everything else during the
forge bootstrap — ``project new`` deliberately ships NO system.yaml, so
``foundry forge`` detects the bootstrap case from project state.

Exit codes: 0 created, 1 refused (already exists / dirty tree), 2
unexpected failure.

:func:`create_project_skeleton` is the shared executor: the CLI command
wraps it for stdout + exit codes; the studio's ``POST /api/projects``
route calls it directly so refusals keep their structured context
(``dirty_files``, ``exists``) instead of a flattened message.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from foundry.core.errors import ConfigValidationError, FoundryError
from foundry.observability.logging import configure_logging
from foundry.versioning.git_backend import GitBackend

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")

_MAX_DIRTY_FILES_LISTED = 20

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


@dataclass(frozen=True)
class ProjectSkeleton:
    """What ``create_project_skeleton`` made."""

    name: str
    project_dir: Path
    branch: str
    commit_sha: str
    files: list[str] = field(default_factory=list)
    """Project-relative paths committed with the skeleton."""


def create_project_skeleton(
    name: str, *, projects_root: Path | None = None
) -> ProjectSkeleton:
    """The shared ``project new`` executor (CLI + studio route).

    Raises :class:`ConfigValidationError` on refusal; the context carries
    the structured reason: ``{"exists": True}`` for an existing project,
    ``{"dirty_files": [...]}`` for an uncommitted working tree (so UIs can
    name the files instead of guessing)."""
    if not _NAME_RE.match(name):
        raise ConfigValidationError(
            f"invalid project name {name!r}; expected "
            "^[a-z][a-z0-9_-]{0,63}$",
            context={"name": name},
        )
    root = (projects_root or Path.cwd() / "projects").resolve()
    project_dir = root / name
    if project_dir.exists():
        raise ConfigValidationError(
            f"project {name!r} already exists at {project_dir}; "
            "refusing to overwrite.",
            context={"name": name, "exists": True},
        )
    backend = GitBackend.discover(root if root.is_dir() else root.parent)
    dirty = [
        line[3:].strip()
        for line in backend.status_porcelain().splitlines()
        if line.strip()
    ]
    if dirty:
        raise ConfigValidationError(
            "working tree has uncommitted changes; commit or stash "
            "before creating a project (the skeleton lands in its own "
            "commit on the new branch). Uncommitted: "
            + ", ".join(dirty[:_MAX_DIRTY_FILES_LISTED])
            + (" ..." if len(dirty) > _MAX_DIRTY_FILES_LISTED else ""),
            context={"name": name, "dirty_files": dirty},
        )
    branch = f"foundry/{name}"
    backend.ensure_branch(branch)
    (project_dir / "evals").mkdir(parents=True)
    (project_dir / "README.md").write_text(_README_TEMPLATE.format(name=name))
    (project_dir / "evals" / ".gitkeep").write_text("")
    commit_sha = backend.commit(
        [
            project_dir / "README.md",
            project_dir / "evals" / ".gitkeep",
        ],
        f"chore({name}): project skeleton (foundry project new)",
    )
    return ProjectSkeleton(
        name=name,
        project_dir=project_dir,
        branch=branch,
        commit_sha=commit_sha,
        files=["README.md", "evals/"],
    )


def execute_project_new(
    name: str, *, projects_root: Path | None = None
) -> int:
    """The `foundry project new` implementation. Returns the exit code."""
    configure_logging()
    try:
        try:
            skeleton = create_project_skeleton(
                name, projects_root=projects_root
            )
        except ConfigValidationError as exc:
            if exc.context.get("exists") or exc.context.get("dirty_files"):
                print(str(exc))
                return 1
            raise
        project_dir = skeleton.project_dir
        print(
            f"Created {project_dir} on branch {skeleton.branch} "
            f"({skeleton.commit_sha[:8]})."
        )
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


__all__ = ["ProjectSkeleton", "create_project_skeleton", "execute_project_new"]
