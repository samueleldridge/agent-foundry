"""Worker identity + drain state (docs/85 § Worker identification).

``worker_id`` is ``hostname:pid`` — the same value the tracing layer
stamps on spans and the runtime EventEmitter stamps on every RunEvent.
This module re-exports it as the API layer's canonical accessor and owns
the per-process :class:`WorkerState` the health endpoint and the graceful
shutdown sequence share (docs/71 § Graceful shutdown step 1: a draining
worker refuses new runs while staying alive for its in-flight ones).

Redis-registered heartbeats (``foundry:workers:<worker_id>`` with TTL) are
the multi-worker discovery mechanism documented in docs/85; v1 documents
the pattern (docs/_manual_tests/phase_8.md) rather than shipping a
heartbeat loop — nothing consumes it until `foundry workers list` (Phase
9 CLI surface).
"""

from __future__ import annotations

import time

from foundry.observability.tracing import worker_id

__all__ = ["WorkerState", "worker_id"]


class WorkerState:
    """Per-process serving state: identity, uptime, drain flag."""

    def __init__(self) -> None:
        self.worker_id = worker_id()
        self.started_monotonic = time.monotonic()
        self.draining = False
        """True once SIGTERM/shutdown began: new runs are refused with
        503 + Retry-After; in-flight runs continue up to the drain
        timeout (docs/71 § Graceful shutdown)."""

    @property
    def uptime_s(self) -> int:
        return int(time.monotonic() - self.started_monotonic)
