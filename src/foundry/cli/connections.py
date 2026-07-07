"""`foundry connections health <project>[/<name>]` — run health.yaml evals.

Target forms:
- ``projects/hello`` — run health checks for every connection the project
  binds; exit non-zero if any fails.
- ``projects/hello/time_service`` — one bound connection by logical name.

Exit codes: 0 all healthy, 1 health failure, 2 config/compile failure.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import httpx

from foundry.config import EnvSecretsProvider, FoundryRoots, load_system_spec
from foundry.config.secrets import SecretsProvider
from foundry.connections import (
    HealthReport,
    prepare_connections,
    run_connection_health,
)
from foundry.core.errors import ConnectionHealthCheckError, FoundryError


def _split_target(target: str) -> tuple[Path, str | None]:
    """'projects/hello/time_service' → (projects/hello, 'time_service');
    'projects/hello' → (projects/hello, None)."""
    candidate = Path(target)
    if (candidate / "system.yaml").exists():
        return candidate, None
    parent = candidate.parent
    if (parent / "system.yaml").exists():
        return parent, candidate.name
    raise FoundryError(
        f"cannot resolve {target!r}: expected <project-dir> or "
        "<project-dir>/<connection-name> where <project-dir> contains "
        "system.yaml",
        context={"target": target},
    )


def _print_report(report: HealthReport) -> None:
    print(f"{report.connection} ({report.ref}): "
          f"{'OK' if report.ok else 'FAIL'}")
    for case in report.cases:
        status = "ok" if case.ok else "FAIL"
        message = f" — {case.message}" if case.message else ""
        print(f"  [{status}] {case.case_id} ({case.latency_ms}ms){message}")


def execute_connections_health(
    target: str,
    *,
    secrets: SecretsProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """The `foundry connections health` implementation."""
    try:
        project_dir, connection_name = _split_target(target)
        system = load_system_spec(project_dir / "system.yaml")
        roots = FoundryRoots.for_project(project_dir)
        prepared = prepare_connections(
            system, roots, secrets or EnvSecretsProvider(),
            system_file=project_dir / "system.yaml",
        )
    except FoundryError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    if connection_name is not None:
        if connection_name not in prepared:
            print(
                f"connection {connection_name!r} is not bound in "
                f"{project_dir / 'system.yaml'} (bound: "
                f"{', '.join(sorted(prepared)) or '(none)'})",
                file=sys.stderr,
            )
            return 2
        prepared = {connection_name: prepared[connection_name]}

    if not prepared:
        print(f"{system.name}: no connections bound; nothing to check")
        return 0

    failures = 0
    for name, connection in prepared.items():
        try:
            report = asyncio.run(
                run_connection_health(
                    connection, project=system.name, http_transport=transport
                )
            )
            _print_report(report)
        except ConnectionHealthCheckError as exc:
            failures += 1
            print(f"{name} ({connection.canonical_ref}): FAIL", file=sys.stderr)
            print(f"  {type(exc).__name__}: {exc}", file=sys.stderr)
            report_dict = exc.context.get("report")
            if report_dict:
                print(f"  details: {json.dumps(report_dict)}", file=sys.stderr)
        except FoundryError as exc:
            failures += 1
            print(f"{name}: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 1 if failures else 0


__all__ = ["execute_connections_health"]
