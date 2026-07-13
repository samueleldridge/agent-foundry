"""Fly.io helper: `fly deploy --image` (docs/84). App identity + regions
come from the fly.toml in the working directory, per fly convention."""

from __future__ import annotations

from foundry.deploy.platforms import CommandPlatform, DeployTarget


class FlyPlatform(CommandPlatform):
    name = "fly"

    def deploy_command(self, target: DeployTarget) -> list[str]:
        return ["fly", "deploy", "--image", target.image]


__all__ = ["FlyPlatform"]
