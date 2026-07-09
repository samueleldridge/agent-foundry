"""Shared context + sandbox for the meta-tools (docs/61 § Sandbox).

Every meta-tool handler closes over one :class:`MetaToolContext` — the
scoped project, the repo's ``GitBackend``, the forge run id, and the
mutable :class:`ForgeRecords` the session loop reads to reconstruct what
the meta-agent actually did each iteration (commits, eval runs, rollbacks,
violations). The records are the session's ground truth: iteration scores
come from RECORDED eval results, never from the meta-agent's self-report.

The sandbox checks here are the structural safety boundary (docs/60
§ Defense in depth): prompt rules are belt; these functions are braces.
A write outside the scoped project (including catalog roots and the
framework tree) — or any write into the project's ``evals/`` (the eval is
the target; the target doesn't move) — is a VIOLATION: it is recorded,
the forge session's cancel token fires, and the run aborts. Recoverable
mistakes (immutable version dir, missing file) raise plain ``ConfigError``
for the meta-agent to read and adapt to.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from foundry.config.refs import FoundryRoots
from foundry.core.errors import ConfigError
from foundry.core.session import Session
from foundry.versioning.git_backend import GitBackend

if TYPE_CHECKING:
    import httpx

    from foundry.config.secrets import SecretsProvider

VIOLATION_CANCEL_PREFIX = "meta_agent.violation"
"""Cancel-token reason prefix for sandbox violations. The session loop
matches on it to terminate the forge with ``sandbox_violation``."""

_IMMUTABLE_KIND_SUBDIRS = ("tools", "connections", "retrievers")


@dataclass
class RecordedCommit:
    sha: str
    message: str
    files: list[str]


@dataclass
class RecordedEval:
    scope: str
    target: str
    eval_run_id: str
    score: float
    passed: bool
    cost_usd: Decimal | None
    eval_spec_path: str


@dataclass
class RecordedRollback:
    scope: str
    target: str
    to_version: str
    commit_sha: str


@dataclass
class RecordedViolation:
    tool: str
    detail: str


@dataclass
class ForgeRecords:
    """Mutable tally of every state-changing meta-tool action."""

    commits: list[RecordedCommit] = field(default_factory=list)
    eval_runs: list[RecordedEval] = field(default_factory=list)
    rollbacks: list[RecordedRollback] = field(default_factory=list)
    violations: list[RecordedViolation] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)

    def mark(self) -> dict[str, int]:
        """Snapshot the record counts (taken before an iteration)."""
        return {
            "commits": len(self.commits),
            "eval_runs": len(self.eval_runs),
            "rollbacks": len(self.rollbacks),
            "violations": len(self.violations),
            "files_written": len(self.files_written),
        }

    def since(self, mark: dict[str, int]) -> ForgeRecords:
        """The activity recorded after ``mark`` (one iteration's slice)."""
        return ForgeRecords(
            commits=self.commits[mark["commits"]:],
            eval_runs=self.eval_runs[mark["eval_runs"]:],
            rollbacks=self.rollbacks[mark["rollbacks"]:],
            violations=self.violations[mark["violations"]:],
            files_written=self.files_written[mark["files_written"]:],
        )


@dataclass
class MetaToolContext:
    """Everything a meta-tool handler needs beyond its typed inputs."""

    scoped_project: str
    projects_root: Path
    framework_root: Path
    catalog_roots: tuple[Path, ...]
    backend: GitBackend
    forge_run_id: str
    transport: httpx.AsyncBaseTransport | None = None
    secrets: SecretsProvider | None = None
    git_email: str | None = None
    records: ForgeRecords = field(default_factory=ForgeRecords)

    @property
    def project_dir(self) -> Path:
        return (self.projects_root / self.scoped_project).resolve()

    @property
    def branch(self) -> str:
        return f"foundry/{self.scoped_project}"

    def roots(self) -> FoundryRoots:
        return FoundryRoots(
            catalog_roots=list(self.catalog_roots),
            projects_root=self.projects_root,
            project_name=self.scoped_project,
        )


# --- sandbox -----------------------------------------------------------------


def resolve_path(ctx: MetaToolContext, raw: str) -> Path:
    """Canonicalise a meta-agent-supplied path. Relative paths resolve
    against the REPO root; symlinks are resolved before any check."""
    path = Path(raw)
    if not path.is_absolute():
        path = ctx.backend.repo_root / path
    return path.resolve()


def _is_under(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def violation(
    ctx: MetaToolContext, session: Session, *, tool: str, detail: str
) -> NoReturn:
    """Record + abort: sandbox violations terminate the forge run (task
    exit gate: writes outside the scoped project raise and abort)."""
    ctx.records.violations.append(RecordedViolation(tool=tool, detail=detail))
    session.cancel_token.cancel(f"{VIOLATION_CANCEL_PREFIX}: {tool}: {detail}")
    raise ConfigError(
        f"{tool}: {detail}",
        context={"tool": tool, "scoped_project": ctx.scoped_project},
    )


def check_read_path(
    ctx: MetaToolContext, session: Session, raw: str, *, tool: str
) -> Path:
    """Reads: scoped project + framework root + catalog roots (docs/61)."""
    path = resolve_path(ctx, raw)
    allowed = (
        ctx.project_dir,
        ctx.framework_root.resolve(),
        *(root.resolve() for root in ctx.catalog_roots),
    )
    if not any(_is_under(path, root) for root in allowed):
        violation(
            ctx,
            session,
            tool=tool,
            detail=f"path outside sandbox: {path} (readable roots: "
            f"{', '.join(str(r) for r in allowed)})",
        )
    return path


def check_write_path(
    ctx: MetaToolContext, session: Session, raw: str, *, tool: str
) -> Path:
    """Writes: STRICTLY inside the scoped project; never ``evals/``;
    never a superseded (frozen) artifact version directory."""
    path = resolve_path(ctx, raw)
    project_dir = ctx.project_dir
    if not _is_under(path, project_dir):
        violation(
            ctx,
            session,
            tool=tool,
            detail=f"write outside the scoped project: {path} (writes are "
            f"limited to {project_dir}; catalog and framework trees are "
            "read-only — catalog promotion is human-gated)",
        )
    relative = path.relative_to(project_dir)
    if relative.parts and relative.parts[0] == "evals":
        violation(
            ctx,
            session,
            tool=tool,
            detail=f"write into the eval set refused: {path} — the eval is "
            "the target; the target doesn't move (docs/60)",
        )
    _check_version_immutability(ctx, path, relative, tool=tool)
    return path


def _check_version_immutability(
    ctx: MetaToolContext, path: Path, relative: Path, *, tool: str
) -> None:
    """Frozen ``v<N>/`` rule (docs/61 § Path immutability): once a LATER
    version exists, earlier version directories are immutable. The live
    (latest) version stays writable — that is how the meta-agent iterates a
    scaffolded handler until its eval passes. ``versions.json`` at the
    artifact level is always writable (metadata legitimately evolves)."""
    parts = relative.parts
    if len(parts) < 4 or parts[0] not in _IMMUTABLE_KIND_SUBDIRS:
        return
    version = parts[2]
    if not version.startswith("v") or not version[1:].isdigit():
        return
    if path.name == "versions.json":
        return
    artifact_root = ctx.project_dir / parts[0] / parts[1]
    numbers = sorted(
        int(child.name[1:])
        for child in artifact_root.iterdir()
        if child.is_dir() and child.name.startswith("v")
        and child.name[1:].isdigit()
    ) if artifact_root.is_dir() else []
    latest = numbers[-1] if numbers else None
    if latest is not None and int(version[1:]) < latest:
        raise ConfigError(
            f"{tool}: {path} is inside frozen version directory "
            f"{parts[0]}/{parts[1]}/{version} (latest is v{latest}); "
            "version directories are immutable once superseded — create "
            "the next version instead (docs/61 § Path immutability)",
            context={"path": str(path), "latest": f"v{latest}"},
        )


__all__ = [
    "VIOLATION_CANCEL_PREFIX",
    "ForgeRecords",
    "MetaToolContext",
    "RecordedCommit",
    "RecordedEval",
    "RecordedRollback",
    "RecordedViolation",
    "check_read_path",
    "check_write_path",
    "resolve_path",
    "violation",
]
