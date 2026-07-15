"""Deploy (dry-run default) — docs/72 § Deploy.

Delegates to the SAME executor `foundry deploy` uses (four phases:
pre-flight → eval gate → platform apply → audit record); structured
params only, never shell strings from the browser. ``dry_run: true`` is
the default so the UI always shows the platform command before applying.
"""

from __future__ import annotations

import io
from contextlib import redirect_stderr, redirect_stdout
from typing import Any

from fastapi import APIRouter

from foundry.studio.context import StudioContext
from foundry.studio.schemas import DeployRequest, DeployResponse, TaskLaunched


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.post("/projects/{name}/deploy", response_model=TaskLaunched)
    async def deploy(name: str, body: DeployRequest) -> TaskLaunched:
        project_dir = ctx.project_dir(name)
        assert ctx.tasks is not None

        def _run() -> dict[str, Any]:
            from foundry.cli.deploy import execute_deploy

            buffer = io.StringIO()
            with redirect_stdout(buffer), redirect_stderr(buffer):
                code = execute_deploy(
                    str(project_dir),
                    image=body.image,
                    target=body.target,
                    platform=body.platform,
                    pre_deploy_eval=body.pre_deploy_eval,
                    production_floor=body.production_floor,
                    dry_run=body.dry_run,
                    skip_eval=body.skip_eval,
                    deployment_name=body.deployment_name,
                    namespace=body.namespace,
                    region=body.region,
                    jobspec=body.jobspec,
                    transport=ctx.transport,
                )
            return DeployResponse(
                project=name,
                dry_run=body.dry_run,
                exit_code=code,
                report=buffer.getvalue()[-20_000:],
            ).model_dump(mode="json")

        task_id = ctx.tasks.launch_sync("deploy", _run)
        return TaskLaunched(
            task_id=task_id, events_url=f"/api/tasks/{task_id}/events"
        )

    return router


__all__ = ["build_router"]
