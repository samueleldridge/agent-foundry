"""Platform helpers for `foundry deploy` (docs/84 § Platform integration).

Thin wrappers around platform CLIs: each helper translates one
:class:`DeployTarget` into the platform's native argv. Helpers WRAP, they do
not replace — operators retain full access to kubectl/aws/gcloud/fly/nomad,
and every argv a helper would run is visible via ``deploy_command`` /
``--dry-run`` before anything executes.
"""

from __future__ import annotations

import subprocess
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from foundry.core.errors import DeployError

_PLATFORM_TIMEOUT_S = 600.0
_STDERR_EXCERPT_CHARS = 500

PlatformName = Literal["kubectl", "ecs", "cloud_run", "fly", "nomad", "noop"]


class DeployTarget(BaseModel):
    """Everything a platform helper needs to roll an image out."""

    model_config = ConfigDict(extra="forbid")

    project: str
    image: str
    platform: str
    namespace: str | None = None
    deployment_name: str | None = None
    region: str | None = None
    service: str | None = None
    extra: dict[str, str] = Field(default_factory=dict)
    """Platform-specific extras (e.g. ``jobspec`` for nomad,
    ``task_definition`` / ``cluster`` for ecs)."""


class PlatformResult(BaseModel):
    """What a helper's ``apply`` did (or would do, under --dry-run)."""

    model_config = ConfigDict(extra="forbid")

    applied: bool
    message: str
    commands: list[list[str]] = Field(default_factory=list)
    """The argv(s) the helper ran (or would run)."""


@runtime_checkable
class PlatformHelper(Protocol):
    """The contract every platform helper satisfies."""

    name: str

    def deploy_command(self, target: DeployTarget) -> list[str]:
        """The primary argv this helper would run for ``target``."""
        ...

    def apply(self, target: DeployTarget, *, dry_run: bool) -> PlatformResult:
        """Execute the rollout (or report it, under ``dry_run``)."""
        ...


class CommandPlatform:
    """Shared subprocess plumbing: subclasses supply the argv translation,
    this base runs it with a timeout and converts every failure into a
    structured :class:`DeployError` (exit code 2 — platform failure)."""

    name: str = ""
    timeout_s: float = _PLATFORM_TIMEOUT_S

    def deploy_command(self, target: DeployTarget) -> list[str]:
        raise NotImplementedError  # pragma: no cover - abstract

    def commands(self, target: DeployTarget) -> list[list[str]]:
        """All argvs ``apply`` runs, in order (kubectl adds a rollout wait)."""
        return [self.deploy_command(target)]

    def apply(self, target: DeployTarget, *, dry_run: bool) -> PlatformResult:
        argvs = self.commands(target)
        if dry_run:
            return PlatformResult(
                applied=False,
                message="dry-run: commands shown, nothing executed",
                commands=argvs,
            )
        outputs: list[str] = []
        for argv in argvs:
            outputs.append(self._run(argv))
        return PlatformResult(
            applied=True,
            message="; ".join(o for o in outputs if o) or "applied",
            commands=argvs,
        )

    def _run(self, argv: list[str]) -> str:
        try:
            completed = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                timeout=self.timeout_s,
                check=False,
            )
        except FileNotFoundError as exc:
            raise DeployError(
                f"platform binary {argv[0]!r} not found on PATH — is the "
                f"{self.name} CLI installed on this runner?",
                context={"exit_code": 2, "platform": self.name, "argv": argv},
                cause=exc,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise DeployError(
                f"{self.name} command timed out after {self.timeout_s}s: "
                f"{' '.join(argv)}",
                context={
                    "exit_code": 2,
                    "platform": self.name,
                    "argv": argv,
                    "timeout_s": self.timeout_s,
                },
                cause=exc,
            ) from exc
        if completed.returncode != 0:
            stderr = completed.stderr.strip()[-_STDERR_EXCERPT_CHARS:]
            raise DeployError(
                f"{self.name} command failed "
                f"(rc {completed.returncode}): {stderr or '<no stderr>'}",
                context={
                    "exit_code": 2,
                    "platform": self.name,
                    "argv": argv,
                    "returncode": completed.returncode,
                    "stderr": stderr,
                },
            )
        return completed.stdout.strip()


def get_platform(name: str) -> PlatformHelper:
    """The helper for ``name`` (hyphen and underscore forms both accepted:
    ``cloud-run`` == ``cloud_run``). Unknown names are a structured refusal
    (exit code 2), never a KeyError."""
    key = name.replace("-", "_")
    # Imported lazily so the submodules can import this module's base
    # classes without a cycle.
    if key == "kubectl":
        from foundry.deploy.platforms.kubectl import KubectlPlatform

        return KubectlPlatform()
    if key == "ecs":
        from foundry.deploy.platforms.ecs import EcsPlatform

        return EcsPlatform()
    if key == "cloud_run":
        from foundry.deploy.platforms.cloud_run import CloudRunPlatform

        return CloudRunPlatform()
    if key == "fly":
        from foundry.deploy.platforms.fly import FlyPlatform

        return FlyPlatform()
    if key == "nomad":
        from foundry.deploy.platforms.nomad import NomadPlatform

        return NomadPlatform()
    if key == "noop":
        from foundry.deploy.platforms.noop import NoopPlatform

        return NoopPlatform()
    raise DeployError(
        f"unknown deploy platform {name!r}; known: kubectl, ecs, cloud-run, "
        "fly, nomad, noop",
        context={"exit_code": 2, "platform": name},
    )


__all__ = [
    "CommandPlatform",
    "DeployTarget",
    "PlatformHelper",
    "PlatformName",
    "PlatformResult",
    "get_platform",
]
