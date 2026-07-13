"""`foundry doctor` unit tests (docs/82 § `foundry doctor` checks).

The happy path runs against the REAL repo root (read-only: doctor loads
configs and probes FOUNDRY_HOME, which the conftest pins to tmp). The
failure path runs against a throwaway projects root under tmp_path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry.cli.doctor import execute_doctor, run_doctor_checks

_REPO_ROOT = Path(__file__).resolve().parents[2]

_ENV_VARS = (
    "FOUNDRY_TRACING",
    "FOUNDRY_CHECKPOINTER",
    "FOUNDRY_RATE_LIMITER",
    "FOUNDRY_STORAGE_BACKEND",
    "FOUNDRY_CATALOG_ROOTS",
)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deterministic environment: no ambient FOUNDRY_* toggles."""
    for name in _ENV_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.unit
def test_doctor_at_repo_root_is_green(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    assert execute_doctor() == 0
    out = capsys.readouterr().out
    assert "framework" in out
    assert "projects_root" in out
    # Unset tracing is a warning, not a failure (docs/82 onboarding output).
    assert "FOUNDRY_TRACING not set (OK for dev)" in out


@pytest.mark.unit
def test_doctor_broken_project_fails_naming_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    broken = tmp_path / "projects" / "broken"
    broken.mkdir(parents=True)
    (broken / "system.yaml").write_text("name: broken\nagents: notalist\n")
    monkeypatch.chdir(tmp_path)
    assert execute_doctor() == 2
    out = capsys.readouterr().out
    assert "config:broken" in out
    assert "FAIL" in out


@pytest.mark.unit
def test_doctor_strict_turns_warnings_into_exit_1(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    # FOUNDRY_TRACING deleted by clean_env → the tracing check warns.
    assert execute_doctor(strict=True) == 1
    assert execute_doctor(strict=False) == 0
    capsys.readouterr()


@pytest.mark.unit
def test_doctor_json_output_parses(
    monkeypatch: pytest.MonkeyPatch,
    clean_env: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    assert execute_doctor(json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert isinstance(payload, list) and payload
    for check in payload:
        assert set(check) == {"name", "status", "detail"}
        assert check["status"] in ("ok", "warn", "fail")
    names = [check["name"] for check in payload]
    assert "tracing" in names
    assert "sandbox" in names


@pytest.mark.unit
def test_doctor_invalid_env_values_fail(
    monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    monkeypatch.setenv("FOUNDRY_CHECKPOINTER", "postgres")
    monkeypatch.setenv("FOUNDRY_STORAGE_BACKEND", "ftp")
    monkeypatch.setenv("FOUNDRY_RATE_LIMITER", "memcached://x")
    monkeypatch.setenv("FOUNDRY_TRACING", "jaeger")
    by_name = {check.name: check for check in run_doctor_checks()}
    assert by_name["checkpointer"].status == "fail"
    assert by_name["storage_backend"].status == "fail"
    assert by_name["rate_limiter"].status == "fail"
    assert by_name["tracing"].status == "fail"


@pytest.mark.unit
def test_doctor_verbose_expands_per_project_checks(
    monkeypatch: pytest.MonkeyPatch, clean_env: None
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    terse = [check.name for check in run_doctor_checks(verbose=False)]
    verbose = [check.name for check in run_doctor_checks(verbose=True)]
    assert "configs" in terse
    assert "config:hello" not in terse
    assert "config:hello" in verbose
