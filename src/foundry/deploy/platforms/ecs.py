"""AWS ECS helper: `aws ecs update-service` (docs/84 § Platform integration).

The new task-definition revision (with the new image baked in) is registered
by the operator's pipeline; this helper points the service at it. Pass the
revision via ``extra.task_definition`` (defaults to the service name — ECS
resolves a bare family name to its latest ACTIVE revision).
"""

from __future__ import annotations

from foundry.deploy.platforms import CommandPlatform, DeployTarget


class EcsPlatform(CommandPlatform):
    name = "ecs"

    def deploy_command(self, target: DeployTarget) -> list[str]:
        service = target.service or target.deployment_name or target.project
        task_definition = target.extra.get("task_definition", service)
        argv = [
            "aws",
            "ecs",
            "update-service",
            "--service",
            service,
            "--task-definition",
            task_definition,
        ]
        if "cluster" in target.extra:
            argv += ["--cluster", target.extra["cluster"]]
        if target.region:
            argv += ["--region", target.region]
        return argv


__all__ = ["EcsPlatform"]
