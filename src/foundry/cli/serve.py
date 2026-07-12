"""`foundry serve <project>` — serve a configured system over HTTP (docs/70).

Single worker: uvicorn runs the app built in-process. ``--workers N``
switches to uvicorn's multiprocess mode, which requires an import-string
factory — the project path (and checkpointer choice) ride the environment
(``FOUNDRY_SERVE_PROJECT``) into each worker, and every worker compiles
its own graph (docs/85 § per-worker compiled objects).

Multi-worker prod shape (docs/85 § Deployment reference configurations):
Postgres checkpointer + Redis rate limiter + LB run_id-hash for
WebSocket. v1 ships sqlite + in-process/Redis rate limiter; the Postgres
checkpointer is documented, not shipped (phase_8 handoff) — with
``--workers N`` today, WebSocket/resume stickiness relies on the shared
FOUNDRY_HOME artifacts + per-project sqlite checkpoint db on one host.

Pre-flight: the project is compiled BEFORE uvicorn boots so a broken
config exits 2 with a structured error instead of N crashing workers.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from foundry.core.errors import FoundryError
from foundry.observability.logging import configure_logging
from foundry.runtime.checkpointers import CHECKPOINTER_CHOICES


def execute_serve(
    project_path: Path,
    *,
    host: str = "127.0.0.1",
    port: int = 8000,
    workers: int = 1,
    checkpoint: str = "sqlite",
    route_prefix: str = "",
) -> int:
    """The `foundry serve` implementation. Returns the process exit code."""
    configure_logging()
    if checkpoint not in CHECKPOINTER_CHOICES:
        print(
            f"--checkpoint must be one of: {', '.join(CHECKPOINTER_CHOICES)} "
            f"(got {checkpoint!r})",
            file=sys.stderr,
        )
        return 2
    if workers < 1:
        print("--workers must be >= 1", file=sys.stderr)
        return 2
    if workers > 1 and checkpoint != "sqlite":
        print(
            "--workers > 1 requires a persistent checkpointer shared "
            "across processes; use --checkpoint sqlite (single host) — "
            "the Postgres checkpointer is the documented multi-host shape "
            "(docs/85), not yet shipped",
            file=sys.stderr,
        )
        return 2

    resolved = project_path.resolve()
    try:
        # Fail fast + report structured errors before uvicorn boots.
        from foundry.runtime.langgraph_adapter import compile_project

        compiled = compile_project(resolved)
    except FoundryError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2

    os.environ["FOUNDRY_SERVE_PROJECT"] = str(resolved)
    os.environ["FOUNDRY_CHECKPOINTER"] = checkpoint
    if route_prefix:
        os.environ["FOUNDRY_ROUTE_PREFIX"] = route_prefix

    print(
        f"serving {compiled.project.system.name} "
        f"(system_version {compiled.system_version[:12]}) "
        f"on http://{host}:{port} with {workers} worker(s)",
        file=sys.stderr,
    )
    import uvicorn

    uvicorn.run(
        "foundry.api.app:create_app_from_env",
        factory=True,
        host=host,
        port=port,
        workers=workers,
        ws="wsproto",
        log_level="info",
    )
    return 0


__all__ = ["execute_serve"]
