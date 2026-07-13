"""`foundry doctor` — environment self-diagnostic (docs/82 § `foundry
doctor` checks).

Every check produces ``(name, status, detail)`` with status ok / warn /
fail. Checks never raise: an unexpected exception inside a check becomes a
fail-status result so the report always completes.

Exit codes (docs/82): 0 all green (warnings tolerated without ``--strict``),
1 warnings under ``--strict``, 2 any hard failure. Useful in CI as a
pre-flight check.

Deliberately cheap: config checks use the full-validation loader
(:func:`foundry.config.loader.load_project` — YAML → extends → env
interpolation → secret scan → Pydantic), and env-var checks validate VALUES
only. Cloud storage backends, Redis limiters, and OTel exporters are NOT
instantiated — doctor reports misconfiguration, it does not open sockets.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Literal

from foundry.core.errors import FoundryError, GitBackendError

CheckStatus = Literal["ok", "warn", "fail"]

_CHECKPOINTER_VALUES = ("memory", "sqlite", "none")
_TRACING_VALUES = ("off", "none", "0", "false", "console", "otel", "langsmith", "langfuse")
_STORAGE_VALUES = ("filesystem", "s3", "s3_compatible", "azure_blob", "gcs")
_RATE_LIMITER_URL_SCHEMES = ("redis://", "rediss://", "unix://")

_MARKS: dict[CheckStatus, str] = {"ok": "✓ OK  ", "warn": "⚠ WARN", "fail": "✗ FAIL"}


@dataclass(frozen=True)
class DoctorCheck:
    name: str
    status: CheckStatus
    detail: str


def _ok(name: str, detail: str) -> DoctorCheck:
    return DoctorCheck(name, "ok", detail)


def _warn(name: str, detail: str) -> DoctorCheck:
    return DoctorCheck(name, "warn", detail)


def _fail(name: str, detail: str) -> DoctorCheck:
    return DoctorCheck(name, "fail", detail)


# --- individual checks ---------------------------------------------------------------


def _check_framework() -> DoctorCheck:
    try:
        version = importlib.metadata.version("agent-foundry")
        detail = f"foundry framework {version} importable"
    except importlib.metadata.PackageNotFoundError:
        detail = "foundry importable (agent-foundry metadata not installed)"
    return _ok("framework", detail)


def _check_python() -> DoctorCheck:
    info = sys.version_info
    version = f"{info.major}.{info.minor}.{info.micro}"
    if (info.major, info.minor) >= (3, 12):
        return _ok("python", f"Python {version} (>= 3.12)")
    return _fail("python", f"Python {version} — foundry requires >= 3.12")


def _projects_root() -> Path:
    return Path.cwd() / "projects"


def _project_names(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(
        child.name
        for child in root.iterdir()
        if (child / "system.yaml").is_file()
    )


def _check_projects_root() -> DoctorCheck:
    root = _projects_root()
    if not root.is_dir():
        return _warn(
            "projects_root",
            f"no projects/ directory under {Path.cwd()} — run doctor from "
            "the repo root (or a tree with a projects/ root)",
        )
    names = _project_names(root)
    listing = ", ".join(names[:8]) + (" …" if len(names) > 8 else "")
    return _ok(
        "projects_root",
        f"{root} — {len(names)} project(s)" + (f": {listing}" if names else ""),
    )


def _check_catalog_roots() -> list[DoctorCheck]:
    env_roots = os.environ.get("FOUNDRY_CATALOG_ROOTS", "").strip()
    if env_roots:
        roots = [Path(part).resolve() for part in env_roots.split(",") if part]
        source = "FOUNDRY_CATALOG_ROOTS"
    else:
        roots = [Path.cwd() / "catalog"]
        source = "./catalog (FOUNDRY_CATALOG_ROOTS unset)"
    checks: list[DoctorCheck] = []
    for root in roots:
        if root.is_dir():
            checks.append(_ok("catalog_roots", f"{root} ({source})"))
        else:
            checks.append(
                _warn(
                    "catalog_roots",
                    f"{root} does not exist ({source}) — catalog/ refs will "
                    "fail to resolve",
                )
            )
    return checks


def _check_project_configs(root: Path, names: list[str]) -> list[DoctorCheck]:
    from foundry.config.loader import load_project

    checks: list[DoctorCheck] = []
    for name in names:
        check_name = f"config:{name}"
        try:
            load_project(root / name)
            checks.append(_ok(check_name, "configs load (full validation)"))
        except FoundryError as exc:
            if "env_var" in exc.context or "environment variable" in str(exc):
                checks.append(
                    _warn(
                        check_name,
                        f"needs environment/secrets to load: {exc}",
                    )
                )
            else:
                checks.append(_fail(check_name, f"{type(exc).__name__}: {exc}"))
    return checks


def _check_secrets_provider() -> DoctorCheck:
    from foundry.config.secrets import EnvSecretsProvider

    provider = EnvSecretsProvider()
    return _ok(
        "secrets_provider",
        f"env (development) — {type(provider).__name__} resolves "
        "kind='env' credentials from os.environ",
    )


def _check_env_file() -> DoctorCheck:
    from foundry.cli.dotenv import find_env_file, parse_env_text

    if os.environ.get("FOUNDRY_NO_ENV_FILE", "").strip():
        return _ok("env_file", "disabled (FOUNDRY_NO_ENV_FILE set) — using process env")
    path = find_env_file()
    if path is None:
        return _ok("env_file", "no .env found — using process env only")
    try:
        keys = [k for k, _ in parse_env_text(path.read_text(encoding="utf-8"))]
    except OSError as exc:
        return _warn("env_file", f"{path} present but unreadable: {exc}")
    # Names only, never values; process env still wins over any of these.
    return _ok(
        "env_file",
        f"{path} — {len(keys)} var(s) auto-loaded by the CLI "
        f"({', '.join(sorted(keys)) or 'none'}); real env wins",
    )


def _check_checkpointer() -> DoctorCheck:
    value = os.environ.get("FOUNDRY_CHECKPOINTER", "").strip()
    if not value:
        return _ok("checkpointer", "FOUNDRY_CHECKPOINTER unset — per-command default")
    if value in _CHECKPOINTER_VALUES:
        return _ok("checkpointer", f"FOUNDRY_CHECKPOINTER={value}")
    return _fail(
        "checkpointer",
        f"FOUNDRY_CHECKPOINTER={value!r} is not recognised; expected one of "
        f"{', '.join(_CHECKPOINTER_VALUES)} (or unset)",
    )


def _check_rate_limiter() -> DoctorCheck:
    value = os.environ.get("FOUNDRY_RATE_LIMITER", "").strip()
    if value in ("", "off", "none"):
        return _ok(
            "rate_limiter",
            f"FOUNDRY_RATE_LIMITER={value or 'unset'} — no provider gate",
        )
    if value == "in_process":
        return _ok("rate_limiter", "FOUNDRY_RATE_LIMITER=in_process (local bucket)")
    if value.startswith(_RATE_LIMITER_URL_SCHEMES):
        return _ok(
            "rate_limiter",
            f"FOUNDRY_RATE_LIMITER={value} (shared bucket; reachability not "
            "probed by doctor)",
        )
    return _fail(
        "rate_limiter",
        f"FOUNDRY_RATE_LIMITER={value!r} is not recognised; expected "
        "'in_process', a redis:// URL, or empty/'off'",
    )


def _check_tracing() -> DoctorCheck:
    raw = os.environ.get("FOUNDRY_TRACING")
    if raw is None or not raw.strip():
        return _warn("tracing", "FOUNDRY_TRACING not set (OK for dev)")
    value = raw.strip().lower()
    if value in _TRACING_VALUES:
        return _ok("tracing", f"FOUNDRY_TRACING={value}")
    return _fail(
        "tracing",
        f"FOUNDRY_TRACING={value!r} is not recognised; expected one of "
        f"{', '.join(_TRACING_VALUES)}",
    )


def _check_home_writable() -> DoctorCheck:
    from foundry.storage.paths import foundry_home

    home = foundry_home()
    probe = home / f".doctor-probe-{os.getpid()}"
    try:
        home.mkdir(parents=True, exist_ok=True)
        probe.touch()
        probe.unlink()
    except OSError as exc:
        return _fail("foundry_home", f"{home} is not writable: {exc}")
    return _ok("foundry_home", f"{home} writable")


def _check_storage_backend() -> DoctorCheck:
    value = os.environ.get("FOUNDRY_STORAGE_BACKEND", "").strip().lower()
    if not value:
        return _ok(
            "storage_backend",
            "FOUNDRY_STORAGE_BACKEND unset — filesystem default",
        )
    if value in _STORAGE_VALUES:
        note = "" if value == "filesystem" else " (cloud backend not instantiated by doctor)"
        return _ok("storage_backend", f"FOUNDRY_STORAGE_BACKEND={value}{note}")
    return _fail(
        "storage_backend",
        f"FOUNDRY_STORAGE_BACKEND={value!r} is not recognised; expected one "
        f"of {', '.join(_STORAGE_VALUES)} (or unset)",
    )


def _check_sandbox() -> DoctorCheck:
    try:
        from foundry.security.sandbox import PathSandbox
    except ImportError as exc:  # pragma: no cover - packaging breakage only
        return _fail("sandbox", f"foundry.security.sandbox not importable: {exc}")
    return _ok("sandbox", f"{PathSandbox.__name__} importable (meta-agent fs boundary)")


def _check_project_git(root: Path, names: list[str]) -> list[DoctorCheck]:
    from foundry.versioning.git_backend import GitBackend

    checks: list[DoctorCheck] = []
    for name in names:
        check_name = f"git:{name}"
        try:
            backend = GitBackend.discover(root / name)
            branch = backend.current_branch()
        except GitBackendError as exc:
            checks.append(
                _warn(check_name, f"not inside a git repository: {exc}")
            )
            continue
        if branch == "HEAD":
            checks.append(
                _warn(
                    check_name,
                    "detached HEAD — check out a branch before versioning "
                    "operations (docs/52 pre-flight)",
                )
            )
        else:
            checks.append(_ok(check_name, f"on branch {branch}"))
    return checks


# --- assembly -------------------------------------------------------------------------


def _guarded(runner: Callable[[], list[DoctorCheck]], name: str) -> list[DoctorCheck]:
    """A check group that throws becomes a fail entry, never a crash."""
    try:
        return runner()
    except Exception as exc:  # doctor must always complete its report
        return [_fail(name, f"check crashed: {type(exc).__name__}: {exc}")]


def _collapse(
    checks: list[DoctorCheck], *, name: str, verbose: bool
) -> list[DoctorCheck]:
    """Without --verbose, all-ok per-project groups collapse into one line;
    warn/fail entries always stay individual."""
    if verbose or not checks:
        return checks
    ok_entries = [c for c in checks if c.status == "ok"]
    rest = [c for c in checks if c.status != "ok"]
    if not ok_entries:
        return rest
    summary = _ok(name, f"{len(ok_entries)}/{len(checks)} ok (--verbose for detail)")
    return [summary, *rest]


def run_doctor_checks(*, verbose: bool = False) -> list[DoctorCheck]:
    """All doctor checks, in the docs/82 order."""
    root = _projects_root()
    names = _project_names(root)
    checks: list[DoctorCheck] = []
    checks.extend(_guarded(lambda: [_check_framework()], "framework"))
    checks.extend(_guarded(lambda: [_check_python()], "python"))
    checks.extend(_guarded(lambda: [_check_projects_root()], "projects_root"))
    checks.extend(_guarded(_check_catalog_roots, "catalog_roots"))
    checks.extend(
        _collapse(
            _guarded(lambda: _check_project_configs(root, names), "configs"),
            name="configs",
            verbose=verbose,
        )
    )
    checks.extend(_guarded(lambda: [_check_secrets_provider()], "secrets_provider"))
    checks.extend(_guarded(lambda: [_check_env_file()], "env_file"))
    checks.extend(_guarded(lambda: [_check_checkpointer()], "checkpointer"))
    checks.extend(_guarded(lambda: [_check_rate_limiter()], "rate_limiter"))
    checks.extend(_guarded(lambda: [_check_tracing()], "tracing"))
    checks.extend(_guarded(lambda: [_check_home_writable()], "foundry_home"))
    checks.extend(_guarded(lambda: [_check_storage_backend()], "storage_backend"))
    checks.extend(_guarded(lambda: [_check_sandbox()], "sandbox"))
    checks.extend(
        _collapse(
            _guarded(lambda: _check_project_git(root, names), "git"),
            name="git",
            verbose=verbose,
        )
    )
    return checks


def execute_doctor(
    *,
    verbose: bool = False,
    strict: bool = False,
    json_output: bool = False,
) -> int:
    """The `foundry doctor` executor. Exit codes: 0 green (warnings pass
    without --strict), 1 warnings under --strict, 2 any failure."""
    checks = run_doctor_checks(verbose=verbose)
    if json_output:
        print(json.dumps([asdict(check) for check in checks], indent=2))
    else:
        width = max(len(check.name) for check in checks)
        for check in checks:
            print(f"{_MARKS[check.status]}  {check.name:<{width}} — {check.detail}")
    has_fail = any(check.status == "fail" for check in checks)
    has_warn = any(check.status == "warn" for check in checks)
    if has_fail:
        return 2
    if has_warn and strict:
        return 1
    return 0


__all__ = ["CheckStatus", "DoctorCheck", "execute_doctor", "run_doctor_checks"]
