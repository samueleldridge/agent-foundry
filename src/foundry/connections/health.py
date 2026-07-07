"""Connection health-check runner (docs/23 § Health checks).

Loads the connection version's health.yaml (an EvalSpec with
``scope: connection``), builds the connection through the pool, and runs the
connection's trivial-operation probe once per case. Full EvalSpec scoring
(scorers, thresholds) arrives with the Phase 4 eval harness; in 2a a case
passes iff the probe returns ok — which is exactly what the CLI needs.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from foundry.config import EvalSpec, load_eval_spec
from foundry.connections.pool import InProcessConnectionPool, SlotConnectionAccessor
from foundry.connections.registry import PreparedConnection
from foundry.core.connection import ConnectionContext
from foundry.core.errors import (
    ConfigValidationError,
    ConnectionHealthCheckError,
    FoundryError,
)


@dataclass(frozen=True)
class HealthCaseResult:
    case_id: str
    ok: bool
    latency_ms: int
    message: str = ""


@dataclass(frozen=True)
class HealthReport:
    connection: str
    ref: str
    ok: bool
    checked_at: datetime
    cases: list[HealthCaseResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "connection": self.connection,
            "ref": self.ref,
            "ok": self.ok,
            "checked_at": self.checked_at.isoformat(),
            "cases": [
                {
                    "case_id": c.case_id,
                    "ok": c.ok,
                    "latency_ms": c.latency_ms,
                    "message": c.message,
                }
                for c in self.cases
            ],
        }


def load_health_spec(prepared: PreparedConnection) -> EvalSpec:
    path = prepared.loaded.health_check_path
    if path is None:
        raise ConnectionHealthCheckError(
            f"connection {prepared.canonical_ref!r} declares no health check "
            "(health_check: null in connection.yaml)",
            context={"connection": prepared.canonical_ref},
        )
    spec = load_eval_spec(path)
    if spec.scope != "connection":
        raise ConfigValidationError(
            f"health check at {path} must declare `scope: connection`; "
            f"got {spec.scope!r}",
            context={"file": str(path), "pointer": "/scope",
                     "received": spec.scope, "expected": "connection"},
        )
    return spec


async def run_connection_health(
    prepared: PreparedConnection,
    *,
    project: str,
    pool: InProcessConnectionPool | None = None,
    http_transport: httpx.AsyncBaseTransport | None = None,
) -> HealthReport:
    """Run the connection's health.yaml. Returns a passing report or raises
    ConnectionHealthCheckError with per-case details (docs/23 failure mode)."""
    spec = load_health_spec(prepared)
    pool = pool or InProcessConnectionPool()
    accessor = SlotConnectionAccessor(
        pool,
        project,
        {"health": prepared},
        ConnectionContext(http_transport=http_transport),
    )
    cases: list[HealthCaseResult] = []
    try:
        try:
            connection = await accessor.get("health")
        except FoundryError as exc:
            raise ConnectionHealthCheckError(
                f"health check for {prepared.canonical_ref!r} could not build "
                f"the connection: {type(exc).__name__}: {exc}",
                context={"connection": prepared.canonical_ref,
                         "cause": exc.to_dict()},
                cause=exc,
            ) from exc

        for case in spec.cases:
            started = time.monotonic()
            try:
                health = await connection.health()
                cases.append(
                    HealthCaseResult(
                        case_id=case.id,
                        ok=health.ok,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        message=health.message,
                    )
                )
            except FoundryError as exc:
                cases.append(
                    HealthCaseResult(
                        case_id=case.id,
                        ok=False,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        message=f"{type(exc).__name__}: {exc}",
                    )
                )
    finally:
        await accessor.release_all()

    report = HealthReport(
        connection=prepared.name,
        ref=prepared.canonical_ref,
        ok=bool(cases) and all(c.ok for c in cases),
        checked_at=datetime.now(UTC),
        cases=cases,
    )
    if not report.ok:
        raise ConnectionHealthCheckError(
            f"health check for {prepared.canonical_ref!r} failed "
            f"({sum(1 for c in cases if not c.ok)}/{len(cases)} case(s) failing)",
            context={"connection": prepared.canonical_ref,
                     "report": report.to_dict()},
        )
    return report


__all__ = ["HealthCaseResult", "HealthReport", "run_connection_health"]
