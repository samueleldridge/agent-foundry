"""Rollback meta-tool (docs/61 § rollback).

Wraps the Phase 5 planners + executor. The meta-agent calls this when
``compare_versions`` shows its latest change regressed. Pre-flight checks
run exactly as they do for humans; a failed check raises ``RollbackError``
with the structured detail, and the meta-agent adapts (no --force class
bypasses are available to it — bypassing safety checks is a human call).
The audit entry is minted with ``kind=meta_agent`` + the forge run id.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict

from foundry.configurator.tools.context import (
    MetaToolContext,
    RecordedRollback,
)
from foundry.core.errors import ConfigError
from foundry.core.tool import RunContext
from foundry.versioning.audit import resolve_operator
from foundry.versioning.rollback import (
    RollbackPlan,
    execute_rollback,
    plan_project_rollback,
    plan_prompt_rollback,
    plan_tool_rollback,
)


class RollbackIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: Literal["tool", "prompt", "project"]
    target: str
    """tool: the system.yaml tools key; prompt: the agent name; project:
    the scoped project's name."""
    to: str
    """v<N> for tool/prompt; a commit ref for project scope."""


class RollbackOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope: str
    target: str
    from_version: str
    to_version: str
    commit_sha: str
    audit_entry_id: str
    notes: list[str]


def make_rollback(
    mctx: MetaToolContext,
) -> Callable[[RollbackIn, RunContext], Awaitable[RollbackOut]]:
    async def handle(inputs: RollbackIn, ctx: RunContext) -> RollbackOut:
        plan: RollbackPlan
        if inputs.scope == "tool":
            plan = plan_tool_rollback(
                mctx.project_dir, inputs.target, inputs.to,
                backend=mctx.backend,
            )
        elif inputs.scope == "prompt":
            plan = plan_prompt_rollback(
                mctx.project_dir, inputs.target, inputs.to,
                backend=mctx.backend,
            )
        else:
            if inputs.target != mctx.scoped_project:
                raise ConfigError(
                    f"rollback: project-scope target must be the scoped "
                    f"project {mctx.scoped_project!r}",
                    context={"target": inputs.target},
                )
            plan = plan_project_rollback(
                mctx.project_dir, inputs.to, backend=mctx.backend
            )
        operator = resolve_operator(
            git_email=mctx.git_email, forge_run_id=mctx.forge_run_id
        )
        # No force / assume_yes: the meta-agent never bypasses pre-flight
        # checks. Failures raise RollbackError with the failing check named.
        result = execute_rollback(
            plan, backend=mctx.backend, operator=operator
        )
        mctx.records.rollbacks.append(
            RecordedRollback(
                scope=inputs.scope,
                target=inputs.target,
                to_version=inputs.to,
                commit_sha=result.commit_sha,
            )
        )
        return RollbackOut(
            scope=inputs.scope,
            target=inputs.target,
            from_version=plan.current,
            to_version=plan.target,
            commit_sha=result.commit_sha,
            audit_entry_id=result.audit_entry.id,
            notes=list(result.notes),
        )

    return handle


__all__ = ["RollbackIn", "RollbackOut", "make_rollback"]
