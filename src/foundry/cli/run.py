"""`foundry run <project-path> --input '...'` — execute a configured system.

Phase 1: compiles a single-agent project, runs it once, prints the typed
output as JSON, and writes the run artifact to ~/.foundry/runs/<run_id>/.
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

from foundry.core import CostBudget, RunId, Session
from foundry.core.errors import FoundryError
from foundry.observability.artifacts import RunArtifactWriter
from foundry.observability.logging import configure_logging, run_logger
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

    try:
        compiled = compile_project(project_path, transport=transport)
    except FoundryError as exc:
        _print_error(exc)
        return 2

    guardrails = compiled.project.system.guardrails
    cost_budget = (
        CostBudget(max_usd=guardrails.max_cost_usd)
        if guardrails.max_cost_usd is not None
        else None
    )
    run_id = RunId.new()
    logger = run_logger(str(run_id))
    session = Session.new(
        project=compiled.project.system.name,
        run_id=run_id,
        cost_budget=cost_budget,
        logger=logger,
        system_version=compiled.system_version,
        pin_set_hash=compiled.pin_set_hash,
    )
    writer = RunArtifactWriter(run_id)
    logger.info(
        "run.starting",
        project=compiled.project.system.name,
        provider=compiled.provider.name,
        model=compiled.provider.model,
        artifact_dir=str(writer.directory),
    )

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
            run_project(compiled, input_data, session, writer.record_event)
        )
    except FoundryError as exc:
        writer.write_metadata(
            project=compiled.project.system.name,
            status="failed",
            provider=compiled.provider.name,
            model=compiled.provider.model,
            error=exc.to_dict(),
            extra={"pins": compiled.pins, **_budget_extra()},
        )
        logger.error("run.failed", error_class=type(exc).__name__)
        _print_error(exc)
        return 1

    writer.write_metadata(
        project=compiled.project.system.name,
        status="completed",
        provider=compiled.provider.name,
        model=compiled.provider.model,
        extra={
            "output": result.output,
            "pins": compiled.pins,
            "connection_pool": result.pool_metrics,
            **_budget_extra(),
        },
    )
    logger.info("run.completed", artifact_dir=str(writer.directory))
    print(json.dumps(result.output, indent=2, default=str))
    return 0


__all__ = ["execute_run"]
