"""HashiCorp Nomad helper: `nomad job run <jobspec>` (docs/84).

The jobspec (already templated with the new image by the operator's
pipeline) rides ``extra.jobspec`` — nomad deploys files, not flags.
"""

from __future__ import annotations

from foundry.core.errors import DeployError
from foundry.deploy.platforms import CommandPlatform, DeployTarget


class NomadPlatform(CommandPlatform):
    name = "nomad"

    def deploy_command(self, target: DeployTarget) -> list[str]:
        jobspec = target.extra.get("jobspec")
        if not jobspec:
            raise DeployError(
                "the nomad platform needs a jobspec path (--jobspec / "
                "extra.jobspec) — `nomad job run` deploys a job file, and "
                "the image lives inside it",
                context={"exit_code": 2, "platform": self.name},
            )
        return ["nomad", "job", "run", jobspec]


__all__ = ["NomadPlatform"]
