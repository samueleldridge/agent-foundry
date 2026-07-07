"""Connections: pool, registry, health runner, descriptor builder (docs/23)."""

from __future__ import annotations

from foundry.connections.descriptors import build_descriptor, config_hash
from foundry.connections.health import (
    HealthCaseResult,
    HealthReport,
    run_connection_health,
)
from foundry.connections.pool import (
    InProcessConnectionPool,
    PoolMetrics,
    SlotConnectionAccessor,
)
from foundry.connections.registry import (
    PreparedConnection,
    prepare_connection,
    prepare_connections,
    resolve_connection_credentials,
    validate_tool_connection_wiring,
)

__all__ = [
    "HealthCaseResult",
    "HealthReport",
    "InProcessConnectionPool",
    "PoolMetrics",
    "PreparedConnection",
    "SlotConnectionAccessor",
    "build_descriptor",
    "config_hash",
    "prepare_connection",
    "prepare_connections",
    "resolve_connection_credentials",
    "run_connection_health",
    "validate_tool_connection_wiring",
]
