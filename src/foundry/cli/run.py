"""`foundry run <project-path> --input '...'` — execute a configured system.

Phase 3: runs through a real LangGraph StateGraph with a checkpointer
attached (`--checkpoint memory|sqlite|none`, default memory). `--stream`
prints every RunEvent to stdout as JSONL the moment it is emitted; the
typed output prints last. `--run-id` reuses a run id — with a sqlite
checkpointer, an interrupted run with that id RESUMES from its last
checkpoint and completes (docs/03 § Phase 3 exit gate).

Structured FoundryErrors print without tracebacks; exit codes: 0 success,
1 run failure, 2 compile/config failure.
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
from foundry.runtime.checkpointers import CHECKPOINTER_CHOICES
from foundry.runtime.langgraph_adapter import compile_project, run_project


def _print_error(exc: FoundryError) -> None:
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    interesting = {
        k: v
        for k, v in exc.context.items()
        if v is not None and k not in ("file", "pointer", "line", "column")
        and f"{k}:" not in str(exc)
    }
    for key, value in interesting.items():
        print(f"  {key}: {value}", file=sys.stderr)


def execute_run(
    project_path: Path,
    input_json: str,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    stream: bool = False,
    checkpoint: str = "memory",
    run_id: str | None = None,
) -> int:
    """The `foundry run` implementation. Returns the process exit code."""
    configure_logging()

    try:
        input_data: dict[str, Any] = json.loads(input_json)
    except json.JSONDecodeError as exc:
        print(f"--input is not valid JSON: {exc}", file=sys.stderr)
        return 2
    if not isinstance(input_data, dict):
        print("--input must be a JSON object, e.g. '{\"name\": \"world\"}'",
              file=sys.stderr)
        return 2
    if checkpoint not in CHECKPOINTER_CHOICES:
        print(
            f"--checkpoint must be one of: {', '.join(CHECKPOINTER_CHOICES)} "
            f"(got {checkpoint!r})",
            file=sys.stderr,
        )
        return 2
    if run_id is not None:
        try:
            resolved_run_id = RunId.validate(run_id)
        except ValueError as exc:
            print(f"--run-id is not a valid run id: {exc}", file=sys.stderr)
            return 2
    else:
        resolved_run_id = RunId.new()

    try:
        compiled = compile_project(project_path, transport=transport)
    except FoundryError as exc:
        _print_error(exc)
        return 2
    for compile_warning in compiled.compile_warnings:
        print(
            f"warning [{compile_warning.category}] "
            f"{compile_warning.message}",
            file=sys.stderr,
        )

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
    logger.info(
        "run.starting",
        project=compiled.project.system.name,
        provider=compiled.provider.name,
        model=compiled.provider.model,
        checkpointer=checkpoint,
        artifact_dir=str(writer.directory),
    )

    def event_sink(event: BaseModel) -> None:
        writer.record_event(event)
        if stream:
            print(event.model_dump_json(), flush=True)

    def _budget_extra() -> dict[str, Any]:
        if cost_budget is None:
            return {}
        return {
            "cost_budget": {
                "max_usd": str(cost_budget.max_usd),
                "accumulated_usd": str(cost_budget.accumulated_usd),
                "remaining_usd": str(cost_budget.remaining_usd()),
            }
        }

    try:
        result = asyncio.run(
            run_project(
                compiled,
                input_data,
                session,
                event_sink,
                checkpointer=checkpoint,
                start_sequence=writer.next_sequence(),
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
                "checkpointer": checkpoint,
                **_budget_extra(),
            },
        )
        logger.error("run.failed", error_class=type(exc).__name__)
        _print_error(exc)
        return 1

    if result.final_state is not None:
        writer.write_final_state(result.final_state)

    if result.status == "approval_pending":
        # HITL pause (docs/32): the checkpointer holds the durable pending
        # state; record it in the artifact so `foundry approvals list` and
        # `foundry resume` can find it without recompiling anything.
        writer.write_metadata(
            project=compiled.project.system.name,
            status="approval_pending",
            provider=compiled.provider.name,
            model=compiled.provider.model,
            extra={
                "pins": compiled.pins,
                "checkpointer": checkpoint,
                "resumed": result.resumed,
                "project_path": str(project_path.resolve()),
                "pending_approval": result.pending_approval,
                **_budget_extra(),
            },
        )
        pending = result.pending_approval or {}
        logger.info(
            "run.approval_pending",
            approval_id=pending.get("approval_id"),
            artifact_dir=str(writer.directory),
        )
        print("run paused: approval required", file=sys.stderr)
        print(f"  run_id:      {resolved_run_id}", file=sys.stderr)
        print(f"  approval_id: {pending.get('approval_id')}", file=sys.stderr)
        print(f"  agent:       {pending.get('agent_name')}", file=sys.stderr)
        print(f"  prompt:      {pending.get('prompt')}", file=sys.stderr)
        if checkpoint != "sqlite":
            print(
                "  WARNING: the checkpointer is not sqlite — this pause "
                "does not survive the process. Re-run with --checkpoint "
                "sqlite for a resumable approval.",
                file=sys.stderr,
            )
        print(
            f"resume with:  foundry resume {resolved_run_id} --approve"
            "  (or --reject --reason '...')",
            file=sys.stderr,
        )
        return 0

    writer.write_metadata(
        project=compiled.project.system.name,
        status="completed",
        provider=compiled.provider.name,
        model=compiled.provider.model,
        extra={
            "output": result.output,
            "pins": compiled.pins,
            "connection_pool": result.pool_metrics,
            "llm_call_count": result.llm_call_count,
            "checkpointer": checkpoint,
            "resumed": result.resumed,
            "run_status": result.status,
            **_budget_extra(),
        },
    )
    logger.info("run.completed", artifact_dir=str(writer.directory))
    print(json.dumps(_deep_jsonable(result.output), indent=2, default=str))
    return 0


def _deep_jsonable(value: Any) -> Any:
    from pydantic import BaseModel

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _deep_jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_deep_jsonable(v) for v in value]
    return value


__all__ = ["execute_run"]
