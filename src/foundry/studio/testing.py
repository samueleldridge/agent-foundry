"""`foundry test` launch + result surface (docs/72 § Projects — the
project-test route). Runs the project's pytest suite (with the
foundry.testing fixtures) as a supervised background task; results via
task polling / task SSE."""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from fastapi import APIRouter

from foundry.studio.context import StudioContext
from foundry.studio.schemas import TaskLaunched


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.post("/projects/{name}/test", response_model=TaskLaunched)
    async def launch_tests(name: str) -> TaskLaunched:
        project_dir = ctx.project_dir(name)
        assert ctx.tasks is not None

        def _run() -> dict[str, Any]:
            from foundry.cli.test import execute_test

            buffer = io.StringIO()
            with redirect_stdout(buffer), redirect_stderr(buffer):
                code = execute_test(str(project_dir), [])
            return {
                "exit_code": code,
                "passed": code == 0,
                "output": buffer.getvalue()[-20_000:],
            }

        task_id = ctx.tasks.launch_sync("test", _run)
        return TaskLaunched(
            task_id=task_id, events_url=f"/api/tasks/{task_id}/events"
        )

    return router


__all__ = ["build_router"]
