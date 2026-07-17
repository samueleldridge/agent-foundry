"""Pre-deploy eval gate + deployment audit recording (docs/84 § Test
expectations 2 & 4): a simulated eval below the floor refuses the deploy
(exit 1, refusal audited); above the floor it proceeds; every `foundry
deploy` invocation produces exactly one deployment audit entry.

Runs against a COPY of projects/hello in tmp_path — audit + eval history
append under ``<project>/.foundry``, which must never dirty the checkout.
All LLM traffic rides httpx.MockTransport (the established no-live-keys
pattern from tests/integration/test_run_hello.py / test_eval_hello.py).
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from foundry.cli.deploy import execute_compute_version, execute_deploy
from foundry.deploy.compute_version import compute_system_version
from foundry.deploy.deploy_recorder import record_deployment
from foundry.deploy.platforms import DeployTarget
from foundry.deploy.pre_deploy_eval import run_pre_deploy_gate
from foundry.versioning.audit import read_audit_entries

REPO_ROOT = Path(__file__).resolve().parents[2]
HELLO_DIR = REPO_ROOT / "projects" / "hello"


@pytest.fixture(autouse=True)
def _isolated_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))
    yield


@pytest.fixture
def hello_copy(tmp_path: Path) -> Path:
    """projects/hello copied out of the checkout (non-repo: the recorder and
    compute-version exercise their no-git fallbacks)."""
    dest = tmp_path / "hello"
    shutil.copytree(HELLO_DIR, dest)
    state = dest / ".foundry"
    if state.exists():
        shutil.rmtree(state)
    return dest


def _greeter_transport(*, name_in_greeting: bool = True) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.openai.com"
        body = json.loads(request.content)
        user_text = next(
            m for m in body["messages"] if m["role"] == "user"
        )["content"]
        name = json.loads(user_text)["name"]
        greeting = (
            f"Hello, {name}! Lovely to meet you."
            if name_in_greeting
            else "Hello there, wonderful stranger!"
        )
        return httpx.Response(
            200,
            json={
                "model": "gpt-5-mini",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"greeting": greeting}),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20},
            },
        )

    return httpx.MockTransport(handler)


def _target(project: str = "hello", **overrides: Any) -> DeployTarget:
    base: dict[str, Any] = {
        "project": project,
        "image": "foundry-hello:cb861da9abcd1234",
        "platform": "noop",
    }
    base.update(overrides)
    return DeployTarget(**base)


# --- pre-deploy gate ---------------------------------------------------------------


@pytest.mark.unit
def test_gate_passes_above_the_floor(hello_copy: Path) -> None:
    gate = run_pre_deploy_gate(
        hello_copy,
        hello_copy / "evals" / "greeting.yaml",
        production_floor=0.9,
        transport=_greeter_transport(),
    )
    assert gate.passed is True
    assert gate.score >= 0.9
    assert gate.floor == 0.9
    assert gate.eval_run_id


@pytest.mark.unit
def test_gate_reports_failure_without_raising(hello_copy: Path) -> None:
    gate = run_pre_deploy_gate(
        hello_copy,
        hello_copy / "evals" / "greeting.yaml",
        production_floor=0.9,
        transport=_greeter_transport(name_in_greeting=False),
    )
    assert gate.passed is False
    assert gate.score < 0.9


# --- recorder -----------------------------------------------------------------------


@pytest.mark.unit
def test_recorder_appends_exactly_one_non_commit_entry(
    hello_copy: Path,
) -> None:
    entry = record_deployment(
        hello_copy,
        target=_target(),
        status="completed",
        detail="applied",
        system_version="cb861da9abcd1234",
    )
    entries = read_audit_entries(hello_copy)
    assert len(entries) == 1
    assert entries[0].id == entry.id
    assert entries[0].type == "non_commit"
    assert entries[0].scope == "hello/deploy"
    assert entries[0].overrides_used == []
    assert "completed" in entries[0].summary
    assert "foundry-hello:cb861da9abcd1234" in entries[0].summary
    assert "cb861da9abcd1234" in entries[0].summary


# --- execute_deploy end-to-end (noop platform) -----------------------------------------


@pytest.mark.unit
def test_deploy_noop_skip_eval_completes_with_one_audit_entry(
    hello_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = execute_deploy(
        str(hello_copy),
        image="foundry-hello:latest",
        platform="noop",
        skip_eval=True,
    )
    assert code == 0
    entries = read_audit_entries(hello_copy)
    assert len(entries) == 1
    assert entries[0].type == "non_commit"
    assert "completed" in entries[0].summary
    out = capsys.readouterr().out
    assert "recorded only (noop platform)" in out


@pytest.mark.unit
def test_deploy_failing_eval_refuses_with_exit_1_and_refused_entry(
    hello_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = execute_deploy(
        str(hello_copy),
        image="foundry-hello:latest",
        platform="noop",
        pre_deploy_eval=str(hello_copy / "evals" / "greeting.yaml"),
        production_floor=0.9,
        transport=_greeter_transport(name_in_greeting=False),
    )
    assert code == 1
    entries = read_audit_entries(hello_copy)
    assert len(entries) == 1
    assert "refused" in entries[0].summary
    assert "REFUSED" in capsys.readouterr().out


@pytest.mark.unit
def test_deploy_passing_eval_completes(hello_copy: Path) -> None:
    code = execute_deploy(
        str(hello_copy),
        image="foundry-hello:latest",
        platform="noop",
        pre_deploy_eval=str(hello_copy / "evals" / "greeting.yaml"),
        production_floor=0.9,
        transport=_greeter_transport(),
    )
    assert code == 0
    entries = read_audit_entries(hello_copy)
    assert len(entries) == 1
    assert "completed" in entries[0].summary


@pytest.mark.unit
def test_deploy_empty_image_is_exit_3(hello_copy: Path) -> None:
    code = execute_deploy(str(hello_copy), image="   ", skip_eval=True)
    assert code == 3
    # pre-flight died before a deploy target existed — nothing to audit
    assert read_audit_entries(hello_copy) == []


@pytest.mark.unit
def test_deploy_version_tagged_image_mismatch_is_exit_4(
    hello_copy: Path,
) -> None:
    """An image tag that CLAIMS to be a system_version hash must match the
    tree being deployed (docs/84 exit 4); the failure is audited."""
    code = execute_deploy(
        str(hello_copy),
        image="foundry-hello:00000000deadbeef",
        platform="noop",
        skip_eval=True,
    )
    assert code == 4
    assert read_audit_entries(hello_copy) == []  # died in pre-flight


@pytest.mark.unit
def test_deploy_matching_version_tag_passes_preflight(
    hello_copy: Path,
) -> None:
    version = compute_system_version(hello_copy)
    code = execute_deploy(
        str(hello_copy),
        image=f"foundry-hello:{version}",
        platform="noop",
        skip_eval=True,
    )
    assert code == 0


@pytest.mark.unit
def test_per_env_config_supplies_defaults_but_cli_flags_win(
    hello_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    deploy_dir = hello_copy / "deploy"
    deploy_dir.mkdir()
    (deploy_dir / "staging.yaml").write_text(
        "target: staging\n"
        "platform: noop\n"
        "deployment_name: hello-staging\n"
        "namespace: staging\n"
        "production_floor: 0.5\n"
    )
    # floor 0.5 from config: the half-failing transport scores below 0.9
    # but the config floor lets it through
    code = execute_deploy(
        str(hello_copy),
        image="foundry-hello:latest",
        target="staging",
        pre_deploy_eval=str(hello_copy / "evals" / "greeting.yaml"),
        transport=_greeter_transport(name_in_greeting=False),
    )
    assert code == 1  # score 0.0 < even the 0.5 floor -> still refused
    out = capsys.readouterr().out
    assert "floor 0.50" in out  # config default applied
    # explicit CLI floor wins over the config value
    code = execute_deploy(
        str(hello_copy),
        image="foundry-hello:latest",
        target="staging",
        pre_deploy_eval=str(hello_copy / "evals" / "greeting.yaml"),
        production_floor=0.95,
        transport=_greeter_transport(),
    )
    assert code == 0
    assert "floor 0.95" in capsys.readouterr().out


# --- compute-version executor -------------------------------------------------------


@pytest.mark.unit
def test_execute_compute_version_prints_the_hash(
    hello_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert execute_compute_version(str(hello_copy)) == 0
    printed = capsys.readouterr().out.strip()
    assert printed == compute_system_version(hello_copy)


@pytest.mark.unit
def test_execute_compute_version_json(
    hello_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert execute_compute_version(str(hello_copy), json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["project"] == "hello"
    assert payload["system_version"] == compute_system_version(hello_copy)


@pytest.mark.unit
def test_execute_compute_version_unknown_project_is_exit_2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert execute_compute_version("no_such_project") == 2
