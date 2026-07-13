"""`foundry test` — project-local pytest wrapper (docs/82 § `foundry test` CLI).

Runs pytest over ``<project>/tests`` with the ``foundry.testing`` plugin
auto-loaded, then (optionally) gates on an eval set.

Exit codes (docs/82, stable for CI): 0 = pass, 1 = test failure,
2 = infrastructure failure, 3 = eval score below threshold.
"""

from __future__ import annotations

import sys
import warnings
from pathlib import Path

import pytest

from foundry.cli._helpers import print_foundry_error, resolve_project_dir
from foundry.core.errors import FoundryError

_PLUGIN = "foundry.testing.pytest_plugin"


def execute_test(
    project: str | None,
    pytest_args: list[str],
    *,
    with_eval: str | None = None,
    fail_under: float | None = None,
) -> int:
    """The `foundry test` implementation. Returns the process exit code."""
    try:
        project_dir = (
            resolve_project_dir(project) if project is not None else Path.cwd()
        )
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2

    tests_dir = project_dir / "tests"
    if not tests_dir.is_dir():
        print(
            f"no tests directory at {tests_dir}; create it per the project "
            "test layout convention (docs/82 § Project test layout)",
            file=sys.stderr,
        )
        return 2

    code = _run_pytest(tests_dir, pytest_args)
    if code != 0:
        print(f"foundry test: FAIL ({tests_dir})", file=sys.stderr)
        return code

    if with_eval is not None:
        eval_code = _run_eval_gate(project_dir, with_eval, fail_under)
        if eval_code != 0:
            return eval_code
        print(f"foundry test: PASS — tests + eval gate ({with_eval})")
        return 0

    print(f"foundry test: PASS ({tests_dir})")
    return 0


def _run_pytest(tests_dir: Path, pytest_args: list[str]) -> int:
    """In-process pytest with the foundry.testing plugin loaded. When the
    plugin module is already imported (repeated in-process runs), pass the
    module object instead of ``-p`` so pytest does not warn that it can no
    longer be rewritten."""
    # Isolate the embedded run from the caller's warning filters (an outer
    # pytest session running with -W error must not turn the inner session's
    # config-time warnings into internal errors); the inner run installs its
    # own filters from the project's config as usual.
    with warnings.catch_warnings():
        warnings.simplefilter("default")
        # pytest-asyncio nags at configure time when a project sets no
        # asyncio_default_fixture_loop_scope; that's the wrapper's invocation
        # detail, not an operator problem — projects that care set it in
        # their own pytest config.
        warnings.filterwarnings(
            "ignore",
            message=".*asyncio_default_fixture_loop_scope.*",
            category=DeprecationWarning,
        )
        if _PLUGIN in sys.modules:
            raw = pytest.main(
                [str(tests_dir), *pytest_args], plugins=[sys.modules[_PLUGIN]]
            )
        else:
            raw = pytest.main([str(tests_dir), "-p", _PLUGIN, *pytest_args])
    return _map_pytest_exit(int(raw))


def _map_pytest_exit(raw: int) -> int:
    """pytest exit codes → foundry test exit codes. 5 (nothing collected) is
    a pass-with-warning; usage/internal/interrupted (2/3/4) are infra."""
    if raw == 0:
        return 0
    if raw == 1:
        return 1
    if raw == 5:
        print(
            "foundry test: no tests collected (treated as pass)", file=sys.stderr
        )
        return 0
    return 2


def _run_eval_gate(
    project_dir: Path, eval_set: str, fail_under: float | None
) -> int:
    """Run the project eval set through the standard `foundry eval` path
    (compile_project + run_eval + threshold check) and remap its exit code:
    below-threshold becomes 3 so CI can tell it apart from a test failure."""
    from foundry.cli.eval import execute_eval

    eval_code = execute_eval(str(project_dir), [eval_set], fail_under=fail_under)
    if eval_code == 0:
        return 0
    if eval_code == 1:
        floor = f" (fail-under {fail_under})" if fail_under is not None else ""
        print(
            f"foundry test: eval {eval_set!r} scored below threshold{floor}",
            file=sys.stderr,
        )
        return 3
    return 2


__all__ = ["execute_test"]
