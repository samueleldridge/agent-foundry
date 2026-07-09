"""Append-only per-project audit log (docs/52 § Audit log format).

``projects/<name>/.foundry/audit.jsonl`` — one JSON object per line, never
edited, never removed. Git is the source of truth for CONTENT; this file is
the source of truth for QUERYABILITY (filtering by type / artifact / time
without shelling out to ``git log``). The file itself is git-versioned, so
post-hoc tampering shows up as a diff (docs/52 § Append-only invariant).

Entry ids are ULIDs (``RunId.new()``) — the versioning operation's run id;
they thread through logs and the ``foundry.rollback`` /
``foundry.catalog.promote`` spans.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from foundry.core.errors import VersioningError
from foundry.core.types import RunId

AuditType = Literal["forge", "human", "rollback", "pin", "catalog", "non_commit"]


class Operator(BaseModel):
    """Who performed the operation (docs/52 § Operator identity capture)."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["meta_agent", "human", "ci"]
    forge_run_id: str | None = None
    human_supervisor: str | None = None
    """Email/username when the meta-agent ran under human supervision."""
    human_email: str | None = None
    """For kind=human; from `git config user.email`, never written by us."""


class EvalContext(BaseModel):
    """Eval movement attached to a change, when known."""

    model_config = ConfigDict(extra="forbid")

    before_score: float | None = None
    before_run_id: str | None = None
    after_score: float | None = None
    after_run_id: str | None = None
    eval_spec_hash: str | None = None


class AuditEntry(BaseModel):
    """One line of ``.foundry/audit.jsonl`` (docs/52 § AuditEntry)."""

    model_config = ConfigDict(extra="forbid")

    id: str
    """ULID; doubles as the versioning operation's run id."""
    timestamp: datetime
    commit_sha: str | None = None
    """None for non-commit operations (cache invalidation, ...)."""
    type: AuditType
    scope: str
    """``<project>/<artifact_path>``."""
    summary: str
    files_affected: list[str] = Field(default_factory=list)
    operator: Operator
    eval: EvalContext | None = None
    cluster_id: str | None = None
    rationale: str | None = None
    overrides_used: list[str] = Field(default_factory=list)
    """Pre-flight checks bypassed via --force / confirmation (docs/52
    § Rollback safety guards) — loud in the log by design."""
    schema_version: Literal[1] = 1


def new_audit_entry(
    *,
    type: AuditType,
    scope: str,
    summary: str,
    operator: Operator,
    commit_sha: str | None = None,
    files_affected: list[str] | None = None,
    eval_context: EvalContext | None = None,
    rationale: str | None = None,
    overrides_used: list[str] | None = None,
) -> AuditEntry:
    """An entry with a fresh ULID id + UTC timestamp."""
    return AuditEntry(
        id=str(RunId.new()),
        timestamp=datetime.now(UTC),
        commit_sha=commit_sha,
        type=type,
        scope=scope,
        summary=summary,
        files_affected=files_affected or [],
        operator=operator,
        eval=eval_context,
        rationale=rationale,
        overrides_used=overrides_used or [],
    )


def audit_log_path(project_dir: Path) -> Path:
    return project_dir / ".foundry" / "audit.jsonl"


def append_audit_entry(project_dir: Path, entry: AuditEntry) -> Path:
    """Append one line. Never rewrites existing content (append mode +
    exclude_none keeps lines compact)."""
    path = audit_log_path(project_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(entry.model_dump(mode="json", exclude_none=True))
    with path.open("a") as handle:
        handle.write(line + "\n")
    return path


def read_audit_entries(
    project_dir: Path,
    *,
    type: AuditType | None = None,
    artifact: str | None = None,
    since: datetime | None = None,
) -> list[AuditEntry]:
    """Parse + filter the project's audit log, oldest first. A torn or
    corrupt line raises — the log is an accountability record; silently
    skipping damage would hide exactly what it exists to expose."""
    path = audit_log_path(project_dir)
    if not path.exists():
        return []
    entries: list[AuditEntry] = []
    for lineno, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            entry = AuditEntry.model_validate(json.loads(line))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise VersioningError(
                f"corrupt audit entry at {path}:{lineno} — the audit log is "
                "append-only JSONL; inspect `git log -p` on the file to see "
                f"what changed: {exc}",
                context={"file": str(path), "line": lineno},
                cause=exc,
            ) from exc
        if type is not None and entry.type != type:
            continue
        if artifact is not None and not _mentions_artifact(entry, artifact):
            continue
        if since is not None and entry.timestamp < since:
            continue
        entries.append(entry)
    return entries


def _mentions_artifact(entry: AuditEntry, artifact: str) -> bool:
    if artifact in entry.scope or artifact in entry.summary:
        return True
    return any(artifact in f for f in entry.files_affected)


def resolve_operator(
    *, git_email: str | None = None, forge_run_id: str | None = None
) -> Operator:
    """Operator identity from context (docs/52 § Operator identity capture).

    Phase 5 surface: humans (CLI) and CI. The meta-agent variant
    (kind=meta_agent + human_supervisor) is minted by Phase 6's forge, which
    passes ``forge_run_id``.
    """
    if forge_run_id is not None:
        return Operator(
            kind="meta_agent",
            forge_run_id=forge_run_id,
            human_supervisor=git_email,
        )
    ci_actor = os.environ.get("GITHUB_ACTOR")
    if ci_actor or os.environ.get("CI"):
        return Operator(kind="ci", human_email=ci_actor or git_email or "unknown")
    return Operator(kind="human", human_email=git_email or "unknown")


__all__ = [
    "AuditEntry",
    "AuditType",
    "EvalContext",
    "Operator",
    "append_audit_entry",
    "audit_log_path",
    "new_audit_entry",
    "read_audit_entries",
    "resolve_operator",
]
