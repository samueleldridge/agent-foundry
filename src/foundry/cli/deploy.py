"""`foundry deploy` / `foundry compute-version` executors (docs/84).

    foundry compute-version <project> [--include-git-sha] [--json]
    foundry deploy <project> --image <ref> [--target dev|staging|prod]
        [--platform kubectl|ecs|cloud-run|fly|nomad|noop]
        [--pre-deploy-eval <path>] [--production-floor 0.90]
        [--dry-run] [--skip-eval] [...]

Exit codes (docs/84, stable): 0 deployed; 1 pre-deploy eval failed
(refusal recorded to audit); 2 platform/config failure; 3 image not
found/empty; 4 manifest system_version mismatch.

Per-environment defaults load from ``projects/<p>/deploy/<target>.yaml``
(platform / deployment_name / namespace / production_floor). CLI flags win:
a config value is used only where the caller left the flag at its default.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import httpx
import yaml

from foundry.cli._helpers import print_foundry_error, resolve_project_dir
from foundry.core.errors import (
    ConfigLoadError,
    ConfigValidationError,
    DeployError,
    FoundryError,
)
from foundry.deploy.compute_version import compute_system_version
from foundry.deploy.deploy_recorder import record_deployment
from foundry.deploy.platforms import DeployTarget, get_platform
from foundry.deploy.pre_deploy_eval import run_pre_deploy_gate
from foundry.observability.logging import configure_logging

_DEFAULT_PLATFORM = "noop"
_DEFAULT_FLOOR = 0.9
_VERSION_TAG_RE = re.compile(r"[0-9a-f]{16}(@[0-9a-f]{4,40})?")


def execute_compute_version(
    project: str, *, include_git_sha: bool = False, json_output: bool = False
) -> int:
    """The `foundry compute-version` implementation."""
    configure_logging()
    try:
        project_dir = resolve_project_dir(project)
        version = compute_system_version(
            project_dir, include_git_sha=include_git_sha
        )
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2
    if json_output:
        print(
            json.dumps(
                {"project": project_dir.name, "system_version": version}
            )
        )
    else:
        print(version)
    return 0


def execute_deploy(
    project: str,
    *,
    image: str,
    target: str = "dev",
    platform: str = _DEFAULT_PLATFORM,
    pre_deploy_eval: str | None = None,
    production_floor: float = _DEFAULT_FLOOR,
    dry_run: bool = False,
    skip_eval: bool = False,
    deployment_name: str | None = None,
    namespace: str | None = None,
    region: str | None = None,
    jobspec: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """The `foundry deploy` implementation. Returns the exit code."""
    configure_logging()
    project_dir: Path | None = None
    deploy_target: DeployTarget | None = None
    system_version = ""
    try:
        # --- 1. pre-flight ------------------------------------------------
        project_dir = resolve_project_dir(project)
        project_name = project_dir.name
        if not image.strip():
            raise DeployError(
                "--image must be a non-empty image reference",
                context={"exit_code": 3, "image": image},
            )
        env_config = _load_env_config(project_dir, target)
        if platform == _DEFAULT_PLATFORM and "platform" in env_config:
            platform = str(env_config["platform"])
        if deployment_name is None and "deployment_name" in env_config:
            deployment_name = str(env_config["deployment_name"])
        if namespace is None and "namespace" in env_config:
            namespace = str(env_config["namespace"])
        if (
            production_floor == _DEFAULT_FLOOR
            and "production_floor" in env_config
        ):
            production_floor = float(env_config["production_floor"])
        system_version = compute_system_version(project_dir)
        _check_manifest_version(image, system_version)
        deploy_target = DeployTarget(
            project=project_name,
            image=image,
            platform=platform,
            namespace=namespace,
            deployment_name=deployment_name,
            region=region,
            extra={"jobspec": jobspec} if jobspec else {},
        )
        print(
            f"[1/4] pre-flight OK — project {project_name}, target {target}, "
            f"platform {platform}, system_version {system_version}"
        )

        # --- 2. pre-deploy eval gate ---------------------------------------
        if pre_deploy_eval and not skip_eval:
            print(
                f"[2/4] pre-deploy eval: {pre_deploy_eval} "
                f"(floor {production_floor:.2f})"
            )
            gate = run_pre_deploy_gate(
                project_dir,
                Path(pre_deploy_eval),
                production_floor=production_floor,
                transport=transport,
            )
            if not gate.passed:
                detail = (
                    f"pre-deploy eval score {gate.score:.2f} below floor "
                    f"{gate.floor:.2f} (eval run {gate.eval_run_id})"
                )
                record_deployment(
                    project_dir,
                    target=deploy_target,
                    status="refused",
                    detail=detail,
                    system_version=system_version,
                )
                print(f"REFUSED: {detail} — nothing deployed (exit 1)")
                return 1
            print(
                f"      gate passed: score {gate.score:.2f} >= "
                f"floor {gate.floor:.2f} (eval run {gate.eval_run_id})"
            )
        else:
            reason = "--skip-eval" if skip_eval else "no --pre-deploy-eval"
            print(f"[2/4] pre-deploy eval skipped ({reason})")

        # --- 3. apply -------------------------------------------------------
        helper = get_platform(platform)
        result = helper.apply(deploy_target, dry_run=dry_run)
        for argv in result.commands:
            print("      $ " + " ".join(argv))
        print(f"[3/4] {result.message}")

        # --- 4. record --------------------------------------------------------
        detail = "dry-run (nothing applied)" if dry_run else result.message
        entry = record_deployment(
            project_dir,
            target=deploy_target,
            status="completed",
            detail=detail,
            system_version=system_version,
        )
        print(f"[4/4] deployment recorded to audit ({entry.id})")
        return 0
    except DeployError as exc:
        exit_code = int(exc.context.get("exit_code", 2))
        _record_failure(project_dir, deploy_target, exc, system_version)
        print_foundry_error(exc)
        return exit_code
    except FoundryError as exc:
        _record_failure(project_dir, deploy_target, exc, system_version)
        print_foundry_error(exc)
        return 2


def _load_env_config(project_dir: Path, target: str) -> dict[str, Any]:
    """``projects/<p>/deploy/<target>.yaml`` when present (docs/84
    § Multi-environment workflows); {} otherwise."""
    path = project_dir / "deploy" / f"{target}.yaml"
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigLoadError(
            f"per-environment deploy config {path} is not valid YAML: {exc}",
            context={"file": str(path)},
            cause=exc,
        ) from exc
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ConfigValidationError(
            f"per-environment deploy config {path} must be a mapping",
            context={"file": str(path), "received": type(loaded).__name__},
        )
    return loaded


def _check_manifest_version(image: str, system_version: str) -> None:
    """docs/84 pre-flight: when the image tag IS a system_version hash, it
    must match the tree being deployed (exit 4 on mismatch). Non-hash tags
    (``latest``, git shas, semver) pass through — the invariant only binds
    tags that claim to be content hashes."""
    last_segment = image.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return
    tag = last_segment.split(":", 1)[1]
    if not _VERSION_TAG_RE.fullmatch(tag):
        return
    tagged_hash = tag.split("@", 1)[0]
    if tagged_hash != system_version:
        raise DeployError(
            f"image tag {tag!r} does not match the project tree's "
            f"system_version {system_version!r} — the image was built from "
            "different config state (rerun compute-version on the state the "
            "image was built from)",
            context={
                "exit_code": 4,
                "image": image,
                "image_system_version": tagged_hash,
                "tree_system_version": system_version,
            },
        )


def _record_failure(
    project_dir: Path | None,
    deploy_target: DeployTarget | None,
    exc: FoundryError,
    system_version: str,
) -> None:
    """Best-effort failure audit (docs/84: audit the failure, do NOT roll
    back). Skipped when pre-flight died before a target existed."""
    if project_dir is None or deploy_target is None:
        return
    try:
        record_deployment(
            project_dir,
            target=deploy_target,
            status="failed",
            detail=f"{type(exc).__name__}: {exc}",
            system_version=system_version,
        )
    except FoundryError:  # pragma: no cover - audit must not mask the error
        pass


__all__ = ["execute_compute_version", "execute_deploy"]
