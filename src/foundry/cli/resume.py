"""`foundry resume <run_id> [--approve | --reject --reason "..."]` and
`foundry approvals list` — the HITL operator surface (docs/32).

A paused run's artifact (``~/.foundry/runs/<run_id>/metadata.json``)
records ``status: approval_pending``, the pending ``InterruptPayload``,
the project path, and the checkpointer choice. Resume recompiles the
project, reuses the run id (the graph thread id), and hands the operator's
decision to the runtime as an ``approval_response`` — the checkpointer
replays the paused node with the resolution threaded into RunContext.
Event sequence numbers continue from the artifact (docs/32 § Resume).

`foundry resume <run_id>` without a decision prints the pending approval.
Exit codes: 0 resumed to completion (or showed pending), 1 run failure,
2 config/usage error.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel

from foundry.core import CostBudget, RunId, Session
from foundry.core.errors import FoundryError
from foundry.observability.artifacts import RunArtifactWriter
from foundry.observability.logging import configure_logging, run_logger
from foundry.runtime.langgraph_adapter import compile_project, run_project
from foundry.storage.paths import run_dir, runs_root


def _read_metadata(run_id: str) -> dict[str, Any] | None:
    path = run_dir(run_id) / "metadata.json"
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def _print_pending(run_id: str, metadata: dict[str, Any]) -> None:
    pending = metadata.get("pending_approval") or {}
    print(f"run {run_id}: approval pending")
    print(f"  approval_id: {pending.get('approval_id')}")
    print(f"  agent:       {pending.get('agent_name')}")
    print(f"  tool:        {pending.get('tool_ref')}")
    print(f"  prompt:      {pending.get('prompt')}")
    context = pending.get("context") or {}
    if context:
        print("  context:")
        for key, value in context.items():
            print(f"    {key}: {value}")
    print(
        f"resolve with: foundry resume {run_id} --approve"
        "  (or --reject --reason '...')"
    )


def execute_resume(
    run_id: str,
    *,
    approve: bool = False,
    reject: bool = False,
    reason: str | None = None,
    project: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """The `foundry resume` implementation. Returns the process exit code."""
    configure_logging()
    try:
        resolved_run_id = RunId.validate(run_id)
    except ValueError as exc:
        print(f"run_id is not valid: {exc}", file=sys.stderr)
        return 2
    if approve and reject:
        print("--approve and --reject are mutually exclusive", file=sys.stderr)
        return 2
    if reject and not reason:
        print("--reject requires --reason (docs/32: rejections carry the "
              "operator's reason back to the agent)", file=sys.stderr)
        return 2

    metadata = _read_metadata(run_id) or {}
    if not approve and not reject:
        if metadata.get("status") == "approval_pending":
            _print_pending(run_id, metadata)
            return 0
        print(
            f"run {run_id} has no pending approval on record "
            f"(status: {metadata.get('status', 'unknown')}); to resume an "
            "interrupted run, rerun `foundry run <project> --run-id "
            f"{run_id} --checkpoint sqlite`",
            file=sys.stderr,
        )
        return 2

    project_path = project or (
        Path(metadata["project_path"])
        if metadata.get("project_path")
        else None
    )
    if project_path is None:
        print(
            f"cannot locate the project for run {run_id}: the run artifact "
            "records no project_path; pass --project <path>",
            file=sys.stderr,
        )
        return 2
    checkpoint = str(metadata.get("checkpointer", "sqlite"))
    if checkpoint != "sqlite":
        print(
            f"run {run_id} was checkpointed with {checkpoint!r}; only "
            "sqlite checkpoints survive the process — this run cannot be "
            "resumed",
            file=sys.stderr,
        )
        return 2
    pending = metadata.get("pending_approval") or {}
    approval_id = str(pending.get("approval_id", ""))
    if not approval_id:
        print(
            f"run {run_id} records no pending approval_id; nothing to "
            "approve or reject",
            file=sys.stderr,
        )
        return 2

    try:
        compiled = compile_project(project_path, transport=transport)
    except FoundryError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    guardrails = compiled.project.system.guardrails
    cost_budget = (
        CostBudget(max_usd=guardrails.max_cost_usd)
        if guardrails.max_cost_usd is not None
        else None
    )
    logger = run_logger(str(resolved_run_id))
    session = Session.new(
        project=compiled.project.system.name,
        run_id=resolved_run_id,
        cost_budget=cost_budget,
        logger=logger,
        system_version=compiled.system_version,
        pin_set_hash=compiled.pin_set_hash,
    )
    writer = RunArtifactWriter(resolved_run_id)
    decision = "approved" if approve else "rejected"
    logger.info(
        "run.resuming",
        project=compiled.project.system.name,
        approval_id=approval_id,
        decision=decision,
    )

    def event_sink(event: BaseModel) -> None:
        writer.record_event(event)

    try:
        result = asyncio.run(
            run_project(
                compiled,
                {},
                session,
                event_sink,
                checkpointer="sqlite",
                start_sequence=writer.next_sequence(),
                approval_response={
                    "approval_id": approval_id,
                    "decision": decision,
                    "reason": reason,
                },
            )
        )
    except FoundryError as exc:
        writer.write_metadata(
            project=compiled.project.system.name,
            status="failed",
            provider=compiled.provider.name,
            model=compiled.provider.model,
            error=exc.to_dict(),
            extra={
                "pins": compiled.pins,
                "checkpointer": "sqlite",
                "project_path": str(project_path.resolve()),
            },
        )
        logger.error("run.failed", error_class=type(exc).__name__)
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    if result.final_state is not None:
        writer.write_final_state(result.final_state)

    if result.status == "approval_pending":
        # Another approval fired downstream — persist the new pending state.
        writer.write_metadata(
            project=compiled.project.system.name,
            status="approval_pending",
            provider=compiled.provider.name,
            model=compiled.provider.model,
            extra={
                "pins": compiled.pins,
                "checkpointer": "sqlite",
                "resumed": True,
                "project_path": str(project_path.resolve()),
                "pending_approval": result.pending_approval,
            },
        )
        next_pending = result.pending_approval or {}
        print("run paused again: another approval required", file=sys.stderr)
        print(
            f"  approval_id: {next_pending.get('approval_id')}",
            file=sys.stderr,
        )
        print(f"  prompt:      {next_pending.get('prompt')}", file=sys.stderr)
        return 0

    writer.write_metadata(
        project=compiled.project.system.name,
        status="completed",
        provider=compiled.provider.name,
        model=compiled.provider.model,
        extra={
            "output": result.output,
            "pins": compiled.pins,
            "checkpointer": "sqlite",
            "resumed": result.resumed,
            "run_status": result.status,
            "project_path": str(project_path.resolve()),
            "resolved_approval": {
                "approval_id": approval_id,
                "decision": decision,
                "reason": reason,
            },
        },
    )
    logger.info("run.completed", artifact_dir=str(writer.directory))
    print(json.dumps(_deep_jsonable(result.output), indent=2, default=str))
    return 0


def execute_approvals_list(project: str | None = None) -> int:
    """`foundry approvals list [<project>]` — pending approvals across the
    local run artifacts (docs/32 § Approval surface)."""
    root = runs_root()
    rows: list[tuple[str, str, str, str]] = []
    if root.is_dir():
        for directory in sorted(root.iterdir()):
            metadata = _read_metadata(directory.name)
            if not metadata or metadata.get("status") != "approval_pending":
                continue
            if project and metadata.get("project") != project:
                continue
            pending = metadata.get("pending_approval") or {}
            rows.append(
                (
                    directory.name,
                    str(metadata.get("project", "?")),
                    str(pending.get("approval_id", "?")),
                    str(pending.get("prompt", ""))[:60],
                )
            )
    if not rows:
        print("no pending approvals")
        return 0
    print(f"{'RUN_ID':<28} {'PROJECT':<16} {'APPROVAL_ID':<32} PROMPT")
    for run_id, project_name, approval_id, prompt in rows:
        print(f"{run_id:<28} {project_name:<16} {approval_id:<32} {prompt}")
    return 0


def _deep_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _deep_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_deep_jsonable(v) for v in value]
    return value


__all__ = ["execute_approvals_list", "execute_resume"]
