"""No-op helper (docs/84): records the deployment in audit + observability
but applies nothing. For custom platforms + pipelines where the operator's
own tooling does the rollout and foundry contributes the gate + the record.
"""

from __future__ import annotations

from foundry.deploy.platforms import CommandPlatform, DeployTarget, PlatformResult


class NoopPlatform(CommandPlatform):
    name = "noop"

    def deploy_command(self, target: DeployTarget) -> list[str]:
        return []

    def commands(self, target: DeployTarget) -> list[list[str]]:
        return []

    def apply(self, target: DeployTarget, *, dry_run: bool) -> PlatformResult:
        # Never shells out — dry-run or not, there is nothing to run.
        return PlatformResult(
            applied=False, message="recorded only (noop platform)"
        )


__all__ = ["NoopPlatform"]
