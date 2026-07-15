"""Doctor checks as structured JSON (docs/72 § Doctor) — same checks,
same order as ``foundry doctor --json``."""

from __future__ import annotations

import anyio
from fastapi import APIRouter

from foundry.studio.context import StudioContext
from foundry.studio.schemas import DoctorCheckModel, DoctorReport


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.get("/doctor", response_model=DoctorReport)
    async def doctor() -> DoctorReport:
        from foundry.cli.doctor import run_doctor_checks

        # The check suite shells out to git and probes the filesystem —
        # off the event loop.
        checks = await anyio.to_thread.run_sync(
            lambda: run_doctor_checks(verbose=True)
        )
        models = [
            DoctorCheckModel(
                check=check.name, status=check.status, detail=check.detail
            )
            for check in checks
        ]
        return DoctorReport(
            checks=models,
            ok=not any(check.status == "fail" for check in models),
        )

    return router


__all__ = ["build_router"]
