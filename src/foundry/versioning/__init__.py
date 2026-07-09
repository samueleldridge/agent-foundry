"""foundry.versioning — git backbone, per-artifact versions, pins, rollback,
audit (docs/50-52).

Public surface (what Phase 6's meta-agent wraps as tools and the CLI calls):
``GitBackend``, the artifact version I/O helpers, ``PinTransaction``, the
rollback planners + executor, and the audit reader/writer.
"""

from __future__ import annotations

from foundry.versioning.artifacts import (
    append_version_metadata,
    artifact_dir,
    create_next_version_dir,
    list_artifact_versions,
    list_prompt_versions,
    next_prompt_path,
    next_version_name,
    prompts_dir,
    read_versions_metadata,
    write_versions_metadata,
)
from foundry.versioning.audit import (
    AuditEntry,
    EvalContext,
    Operator,
    append_audit_entry,
    audit_log_path,
    new_audit_entry,
    read_audit_entries,
    resolve_operator,
)
from foundry.versioning.compat import (
    ContractDiff,
    connection_contract_diff,
    tool_contract_diff,
)
from foundry.versioning.git_backend import CommitInfo, GitBackend
from foundry.versioning.pins import (
    PinChange,
    PinTransaction,
    read_connection_pin,
    read_prompt_pin,
    read_tool_pin,
)
from foundry.versioning.refs import (
    check_version_contiguity,
    latest_version,
    parse_artifact_ref,
    resolve_version_dir,
)
from foundry.versioning.rollback import (
    PreflightCheck,
    RollbackPlan,
    RollbackResult,
    execute_rollback,
    plan_project_rollback,
    plan_prompt_rollback,
    plan_tool_rollback,
)

__all__ = [
    "AuditEntry",
    "CommitInfo",
    "ContractDiff",
    "EvalContext",
    "GitBackend",
    "Operator",
    "PinChange",
    "PinTransaction",
    "PreflightCheck",
    "RollbackPlan",
    "RollbackResult",
    "append_audit_entry",
    "append_version_metadata",
    "artifact_dir",
    "audit_log_path",
    "check_version_contiguity",
    "connection_contract_diff",
    "create_next_version_dir",
    "execute_rollback",
    "latest_version",
    "list_artifact_versions",
    "list_prompt_versions",
    "new_audit_entry",
    "next_prompt_path",
    "next_version_name",
    "parse_artifact_ref",
    "plan_project_rollback",
    "plan_prompt_rollback",
    "plan_tool_rollback",
    "prompts_dir",
    "read_audit_entries",
    "read_connection_pin",
    "read_prompt_pin",
    "read_tool_pin",
    "read_versions_metadata",
    "resolve_operator",
    "resolve_version_dir",
    "tool_contract_diff",
    "write_versions_metadata",
]
