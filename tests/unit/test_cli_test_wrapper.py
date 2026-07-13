"""`foundry test` CLI wrapper: exit codes + plugin auto-load (docs/82)."""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.cli.test import execute_test

_PASSING_TEST = """\
def test_always_passes() -> None:
    assert 1 + 1 == 2
"""

_FAILING_TEST = """\
def test_always_fails() -> None:
    assert 1 + 1 == 3
"""

_FIXTURE_TEST = """\
def test_run_context_fixture_is_auto_loaded(run_context) -> None:
    # provided by foundry.testing.pytest_plugin via `foundry test`'s -p flag;
    # no conftest in this project declares it.
    assert run_context.run_id == "test-run"
    assert run_context.agent_name == "test_agent"
    assert run_context.tool_ref == "local/test_tool@v1"
"""


def _make_project(root: Path, name: str, test_files: dict[str, str]) -> Path:
    """A minimal on-disk project: system.yaml marker + tests/ tree."""
    project = root / name
    tests = project / "tests"
    tests.mkdir(parents=True)
    (project / "system.yaml").write_text("# minimal marker for resolve_project_dir\n")
    for filename, body in test_files.items():
        (tests / filename).write_text(body)
    return project


@pytest.mark.unit
def test_execute_test_returns_0_when_tests_pass(tmp_path: Path) -> None:
    project = _make_project(
        tmp_path, "proj_pass", {"test_wrapper_case_pass.py": _PASSING_TEST}
    )
    assert execute_test(str(project), ["-q"]) == 0


@pytest.mark.unit
def test_execute_test_returns_1_when_tests_fail(tmp_path: Path) -> None:
    project = _make_project(
        tmp_path, "proj_fail", {"test_wrapper_case_fail.py": _FAILING_TEST}
    )
    assert execute_test(str(project), ["-q"]) == 1


@pytest.mark.unit
def test_execute_test_missing_tests_dir_is_infra_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "proj_no_tests"
    project.mkdir()
    (project / "system.yaml").write_text("# marker\n")
    assert execute_test(str(project), []) == 2
    assert "no tests directory" in capsys.readouterr().err


@pytest.mark.unit
def test_execute_test_unknown_project_is_infra_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert execute_test(str(tmp_path / "does_not_exist"), []) == 2
    assert "not found" in capsys.readouterr().err


@pytest.mark.unit
def test_plugin_fixtures_auto_load_via_dash_p(tmp_path: Path) -> None:
    """A project test uses the `run_context` fixture without any conftest —
    proving the foundry.testing plugin is loaded by the wrapper itself."""
    project = _make_project(
        tmp_path, "proj_fixture", {"test_wrapper_case_fixture.py": _FIXTURE_TEST}
    )
    assert execute_test(str(project), ["-q"]) == 0


@pytest.mark.unit
def test_empty_tests_dir_passes_with_warning(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _make_project(tmp_path, "proj_empty", {})
    assert execute_test(str(project), ["-q"]) == 0
    assert "no tests collected" in capsys.readouterr().err


@pytest.mark.unit
@pytest.mark.parametrize(
    ("eval_exit", "expected"),
    [(0, 0), (1, 3), (2, 2)],  # below-threshold remaps to 3; infra stays 2
)
def test_with_eval_gate_remaps_eval_exit_codes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    eval_exit: int,
    expected: int,
) -> None:
    project = _make_project(
        tmp_path,
        f"proj_eval_{eval_exit}",
        {f"test_wrapper_case_eval_{eval_exit}.py": _PASSING_TEST},
    )
    seen: dict[str, object] = {}

    def fake_execute_eval(
        target: str, args: list[str], *, fail_under: float | None = None, **_: object
    ) -> int:
        seen["target"] = target
        seen["args"] = args
        seen["fail_under"] = fail_under
        return eval_exit

    monkeypatch.setattr("foundry.cli.eval.execute_eval", fake_execute_eval)
    code = execute_test(
        str(project), ["-q"], with_eval="evals/smoke.yaml", fail_under=0.9
    )
    assert code == expected
    assert seen["target"] == str(project)
    assert seen["args"] == ["evals/smoke.yaml"]
    assert seen["fail_under"] == 0.9
