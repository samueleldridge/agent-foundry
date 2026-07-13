"""Audit + observability for deployments (docs/84 § `foundry deploy` step 5).

Invariant 6: deployment metadata is ALWAYS recorded — completed, failed, or
refused — in the project's append-only audit log. No silent rollouts. A
deployment is a ``non_commit`` audit type: it changes what's RUNNING, not
what's committed (config commits are the forge/rollback/pin types).
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from foundry.core.errors import GitBackendError
from foundry.deploy.platforms import DeployTarget
from foundry.observability.logging import run_logger
from foundry.versioning.audit import (
    AuditEntry,
    append_audit_entry,
    new_audit_entry,
    resolve_operator,
)
from foundry.versioning.git_backend import GitBackend

DeploymentStatus = Literal["completed", "failed", "refused"]


def record_deployment(
    project_dir: Path,
    *,
    target: DeployTarget,
    status: DeploymentStatus,
    detail: str,
    system_version: str,
) -> AuditEntry:
    """Append exactly one deployment entry to the project audit log and emit
    the matching structured log line. Returns the entry (its ULID id is the
    deployment operation's run id)."""
    commit_sha: str | None = None
    git_email: str | None = None
    try:
        backend = GitBackend.discover(project_dir)
        commit_sha = backend.rev_parse("HEAD")
        git_email = backend.user_email()
    except GitBackendError:
        # Non-repo deploy contexts (e.g. an unpacked release tree) still
        # get their audit entry — commit provenance is just unavailable.
        pass
    entry = new_audit_entry(
        type="non_commit",
        scope=f"{target.project}/deploy",
        summary=(
            f"deployment {status}: image {target.image} via "
            f"{target.platform} (system_version {system_version}) — {detail}"
        ),
        operator=resolve_operator(git_email=git_email),
        commit_sha=commit_sha,
        overrides_used=[],
    )
    append_audit_entry(project_dir, entry)
    run_logger(entry.id).info(
        f"deployment.{status}",
        project=target.project,
        image=target.image,
        platform=target.platform,
        system_version=system_version,
        detail=detail,
    )
    # metrics: foundry.deployment counter — wired in observability
    return entry


__all__ = ["DeploymentStatus", "record_deployment"]
