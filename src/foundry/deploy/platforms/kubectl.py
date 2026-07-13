"""Kubernetes helper: `kubectl set image` + rollout-status wait (docs/84)."""

from __future__ import annotations

from foundry.deploy.platforms import CommandPlatform, DeployTarget


class KubectlPlatform(CommandPlatform):
    name = "kubectl"

    @staticmethod
    def _deployment(target: DeployTarget) -> str:
        return target.deployment_name or target.project

    def deploy_command(self, target: DeployTarget) -> list[str]:
        name = self._deployment(target)
        argv = [
            "kubectl",
            "set",
            "image",
            f"deployment/{name}",
            f"{name}={target.image}",
        ]
        if target.namespace:
            argv += ["-n", target.namespace]
        return argv

    def wait_command(self, target: DeployTarget) -> list[str]:
        """Blocks until the rollout completes (docs/84 step 4: wait for
        healthy) — kubectl's own readiness polling, not ours."""
        argv = [
            "kubectl",
            "rollout",
            "status",
            f"deployment/{self._deployment(target)}",
        ]
        if target.namespace:
            argv += ["-n", target.namespace]
        return argv

    def commands(self, target: DeployTarget) -> list[list[str]]:
        return [self.deploy_command(target), self.wait_command(target)]


__all__ = ["KubectlPlatform"]
