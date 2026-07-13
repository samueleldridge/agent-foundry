"""Google Cloud Run helper: `gcloud run deploy` (docs/84)."""

from __future__ import annotations

from foundry.deploy.platforms import CommandPlatform, DeployTarget


class CloudRunPlatform(CommandPlatform):
    name = "cloud_run"

    def deploy_command(self, target: DeployTarget) -> list[str]:
        name = target.service or target.deployment_name or target.project
        argv = ["gcloud", "run", "deploy", name, "--image", target.image]
        if target.region:
            argv += ["--region", target.region]
        return argv


__all__ = ["CloudRunPlatform"]
