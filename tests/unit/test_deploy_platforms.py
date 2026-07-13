"""Platform helper argv translation (docs/84 § Platform integration table)
+ the subprocess discipline: dry-run and noop never shell out; a missing
binary is a structured exit-2 DeployError, never a raw FileNotFoundError.
"""

from __future__ import annotations

from typing import Any, NoReturn

import pytest

import foundry.deploy.platforms as platforms_module
from foundry.core.errors import DeployError
from foundry.deploy.platforms import DeployTarget, get_platform

IMAGE = "foundry-hello:cb861da9abcd1234"


def _target(**overrides: Any) -> DeployTarget:
    base: dict[str, Any] = {
        "project": "hello",
        "image": IMAGE,
        "platform": "noop",
        "deployment_name": "hello",
        "namespace": "prod",
        "region": "europe-west1",
        "extra": {"jobspec": "deploy/nomad/hello.nomad"},
    }
    base.update(overrides)
    return DeployTarget(**base)


@pytest.fixture
def no_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if ANY helper shells out."""

    def forbidden(*args: Any, **kwargs: Any) -> NoReturn:
        raise AssertionError(f"subprocess.run must not be called: {args}")

    monkeypatch.setattr(platforms_module.subprocess, "run", forbidden)


# --- documented argv per platform ---------------------------------------------


@pytest.mark.unit
def test_kubectl_set_image_with_namespace_and_rollout_wait() -> None:
    helper = get_platform("kubectl")
    target = _target(platform="kubectl")
    assert helper.deploy_command(target) == [
        "kubectl", "set", "image", "deployment/hello",
        f"hello={IMAGE}", "-n", "prod",
    ]
    commands = helper.commands(target)  # type: ignore[attr-defined]
    assert commands[1] == [
        "kubectl", "rollout", "status", "deployment/hello", "-n", "prod",
    ]


@pytest.mark.unit
def test_kubectl_omits_namespace_flag_when_unset() -> None:
    helper = get_platform("kubectl")
    target = _target(platform="kubectl", namespace=None)
    assert helper.deploy_command(target) == [
        "kubectl", "set", "image", "deployment/hello", f"hello={IMAGE}",
    ]


@pytest.mark.unit
def test_ecs_update_service() -> None:
    helper = get_platform("ecs")
    target = _target(
        platform="ecs",
        extra={"task_definition": "hello:42", "cluster": "prod-cluster"},
    )
    assert helper.deploy_command(target) == [
        "aws", "ecs", "update-service",
        "--service", "hello",
        "--task-definition", "hello:42",
        "--cluster", "prod-cluster",
        "--region", "europe-west1",
    ]


@pytest.mark.unit
def test_cloud_run_deploy_accepts_both_spellings() -> None:
    target = _target(platform="cloud-run")
    expected = [
        "gcloud", "run", "deploy", "hello",
        "--image", IMAGE,
        "--region", "europe-west1",
    ]
    assert get_platform("cloud-run").deploy_command(target) == expected
    assert get_platform("cloud_run").deploy_command(target) == expected


@pytest.mark.unit
def test_fly_deploy_image() -> None:
    helper = get_platform("fly")
    assert helper.deploy_command(_target(platform="fly")) == [
        "fly", "deploy", "--image", IMAGE,
    ]


@pytest.mark.unit
def test_nomad_job_run_uses_extra_jobspec() -> None:
    helper = get_platform("nomad")
    assert helper.deploy_command(_target(platform="nomad")) == [
        "nomad", "job", "run", "deploy/nomad/hello.nomad",
    ]


@pytest.mark.unit
def test_nomad_without_jobspec_is_structured_exit_2() -> None:
    helper = get_platform("nomad")
    with pytest.raises(DeployError) as excinfo:
        helper.deploy_command(_target(platform="nomad", extra={}))
    assert excinfo.value.context["exit_code"] == 2


@pytest.mark.unit
def test_unknown_platform_is_structured_exit_2() -> None:
    with pytest.raises(DeployError) as excinfo:
        get_platform("swarm")
    assert excinfo.value.context["exit_code"] == 2
    assert "swarm" in str(excinfo.value)


# --- apply discipline ------------------------------------------------------------


@pytest.mark.unit
def test_noop_apply_never_shells_out(no_subprocess: None) -> None:
    helper = get_platform("noop")
    result = helper.apply(_target(), dry_run=False)
    assert result.applied is False
    assert result.message == "recorded only (noop platform)"
    assert result.commands == []


@pytest.mark.unit
def test_dry_run_returns_commands_without_executing(
    no_subprocess: None,
) -> None:
    helper = get_platform("kubectl")
    result = helper.apply(_target(platform="kubectl"), dry_run=True)
    assert result.applied is False
    assert result.commands[0][:3] == ["kubectl", "set", "image"]
    assert result.commands[1][:3] == ["kubectl", "rollout", "status"]


@pytest.mark.unit
def test_missing_binary_is_structured_exit_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*args: Any, **kwargs: Any) -> NoReturn:
        raise FileNotFoundError("kubectl")

    monkeypatch.setattr(platforms_module.subprocess, "run", missing)
    helper = get_platform("kubectl")
    with pytest.raises(DeployError) as excinfo:
        helper.apply(_target(platform="kubectl"), dry_run=False)
    assert excinfo.value.context["exit_code"] == 2
    assert "kubectl" in str(excinfo.value)


@pytest.mark.unit
def test_nonzero_rc_is_structured_exit_2_with_stderr_excerpt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeCompleted:
        returncode = 1
        stdout = ""
        stderr = "error: deployment 'hello' not found in namespace 'prod'"

    monkeypatch.setattr(
        platforms_module.subprocess,
        "run",
        lambda *a, **k: FakeCompleted(),
    )
    helper = get_platform("kubectl")
    with pytest.raises(DeployError) as excinfo:
        helper.apply(_target(platform="kubectl"), dry_run=False)
    assert excinfo.value.context["exit_code"] == 2
    assert "not found in namespace" in str(excinfo.value)
