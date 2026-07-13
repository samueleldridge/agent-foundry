"""Pre-deploy eval gate (docs/84 § `foundry deploy` step 2).

Runs the smoke eval against the project compiled from the CURRENT tree and
compares the score to the target environment's production floor. The gate
REPORTS — a failing score returns ``GateResult(passed=False)``, it never
raises; the caller decides to refuse (and records the refusal to audit).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict


class GateResult(BaseModel):
    """The pre-deploy gate's verdict."""

    model_config = ConfigDict(extra="forbid")

    score: float
    floor: float
    passed: bool
    eval_run_id: str
    """The persisted eval run backing the verdict — auditable evidence."""


def run_pre_deploy_gate(
    project_dir: Path,
    eval_path: Path,
    *,
    production_floor: float,
    transport: httpx.AsyncBaseTransport | None = None,
) -> GateResult:
    """Compile the project, run the eval set, compare to the floor."""
    from foundry.config import load_eval_spec
    from foundry.eval import ProjectEvalTarget, run_eval
    from foundry.orchestration.compiler import compile_project

    spec = load_eval_spec(eval_path)
    compiled = compile_project(project_dir, transport=transport)
    result = asyncio.run(
        run_eval(
            spec,
            ProjectEvalTarget(compiled),
            transport=transport,
            eval_spec_ref=str(eval_path),
        )
    )
    return GateResult(
        score=result.score,
        floor=production_floor,
        passed=result.score >= production_floor,
        eval_run_id=str(result.eval_run_id),
    )


__all__ = ["GateResult", "run_pre_deploy_gate"]
