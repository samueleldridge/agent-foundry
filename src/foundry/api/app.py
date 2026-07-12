"""FastAPI app factory — one served project, fully introspected (docs/70).

``create_app(project_path)`` compiles the project once, derives the
input/output models from the CompiledProject (no hand-written per-project
routes), and assembles the docs/70 endpoint catalogue with:

- an app-lifespan **task group** every run drives inside (docs/71
  structured concurrency; shutdown = drain → force-cancel → exit);
- the **auth plug-point** (docs/70 § Authentication) on every route
  except ``/health`` / ``/openapi.json`` / ``/docs``;
- a **CORS stub** (off unless origins are configured);
- **response headers** on every response: ``X-Foundry-System-Version``,
  ``X-Foundry-Pin-Set-Hash``, ``X-Foundry-Worker-Id``, plus
  ``X-Request-Id`` propagation;
- structured **error handling**: every error body is a
  ``FoundryError.to_dict()`` shape; never a stack trace.

``create_app_from_env`` is the uvicorn factory string target for
multi-worker serving (``foundry serve --workers N`` passes the project
via ``FOUNDRY_SERVE_PROJECT``; each worker process compiles its own graph
— docs/85 § per-worker compiled objects).
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from foundry.api.auth import AuthBackend, default_auth_backend
from foundry.api.errors import (
    error_body,
    headers_for,
    status_for,
    validation_error_body,
)
from foundry.api.routes import build_routers
from foundry.api.runs import RunManager
from foundry.api.schemas import derive_input_model, derive_output_model
from foundry.api.worker import WorkerState
from foundry.core.errors import FoundryError
from foundry.runtime.langgraph_adapter import compile_project

_FRAMEWORK_VERSION = "0.1.0"  # mirrors pyproject [project].version


def create_app(
    project_path: Path | str,
    *,
    auth_backend: AuthBackend | None = None,
    checkpoint: str = "sqlite",
    checkpoint_db: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    cors_origins: list[str] | None = None,
    max_concurrent_runs: int | None = None,
    drain_timeout_s: float | None = None,
    route_prefix: str = "",
) -> FastAPI:
    """Compile ``project_path`` and return a uvicorn-runnable FastAPI app.

    ``route_prefix`` (e.g. ``"/v1"``) is the docs/70 Pattern-2 URL
    versioning hook. ``transport`` substitutes the provider HTTP layer
    (tests). All construction is eager: a broken project fails HERE, not
    on the first request.
    """
    compiled = compile_project(Path(project_path), transport=transport)
    input_model = derive_input_model(compiled)
    output_model = derive_output_model(compiled)
    worker_state = WorkerState()
    manager = RunManager(
        compiled,
        worker_state=worker_state,
        checkpoint=checkpoint,
        checkpoint_db=checkpoint_db,
        max_concurrent_runs=max_concurrent_runs,
        drain_timeout_s=drain_timeout_s,
    )
    backend = auth_backend if auth_backend is not None else default_auth_backend()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        # The service nursery (docs/71): every run is a child task here,
        # so process shutdown can never orphan one.
        async with anyio.create_task_group() as tg:
            manager.bind(tg)
            try:
                yield
            finally:
                # docs/71 § Graceful shutdown: drain, force-cancel, exit.
                await manager.shutdown()
                tg.cancel_scope.cancel()

    app = FastAPI(
        title=f"foundry: {compiled.project.system.name}",
        version=compiled.system_version,
        description=compiled.project.system.description,
        lifespan=lifespan,
    )
    app.state.manager = manager
    app.state.input_model = input_model
    app.state.output_model = output_model

    protected, open_router = build_routers(
        manager, input_model, output_model, backend, _FRAMEWORK_VERSION
    )
    app.include_router(protected, prefix=route_prefix)
    app.include_router(open_router, prefix=route_prefix)

    if cors_origins:
        from fastapi.middleware.cors import CORSMiddleware

        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=[
                "X-Foundry-Run-Id",
                "X-Foundry-System-Version",
                "X-Foundry-Pin-Set-Hash",
                "X-Foundry-Worker-Id",
            ],
        )

    @app.middleware("http")
    async def foundry_headers(request: Request, call_next: Any) -> Response:
        request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex
        response: Response = await call_next(request)
        response.headers.setdefault("X-Request-Id", request_id)
        response.headers.setdefault(
            "X-Foundry-System-Version", compiled.system_version
        )
        response.headers.setdefault(
            "X-Foundry-Pin-Set-Hash", compiled.pin_set_hash
        )
        response.headers.setdefault(
            "X-Foundry-Worker-Id", worker_state.worker_id
        )
        return response

    @app.exception_handler(FoundryError)
    async def foundry_error_handler(
        request: Request, exc: FoundryError
    ) -> JSONResponse:
        status = status_for(exc)
        return JSONResponse(
            status_code=status,
            content=error_body(exc),
            headers=headers_for(status),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=validation_error_body(
                [dict(e) for e in exc.errors()]
            ),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        # Auth raises HTTPException(detail={"error": ...}) — surface the
        # dict body directly (docs/70 § Failure modes wire shapes).
        content = (
            exc.detail
            if isinstance(exc.detail, dict)
            else {"error": str(exc.detail)}
        )
        return JSONResponse(status_code=exc.status_code, content=content)

    return app


def create_app_from_env() -> FastAPI:
    """uvicorn factory target for multi-worker serving:

    ``uvicorn --factory foundry.api.app:create_app_from_env --workers N``

    with ``FOUNDRY_SERVE_PROJECT=<path>`` (and optionally
    ``FOUNDRY_CHECKPOINTER=sqlite``, ``FOUNDRY_API_TOKENS=...``,
    ``FOUNDRY_CORS_ORIGINS=a,b``). Worker processes cannot share Python
    objects, so configuration rides the environment.
    """
    project = os.environ.get("FOUNDRY_SERVE_PROJECT", "").strip()
    if not project:
        raise FoundryError(
            "FOUNDRY_SERVE_PROJECT is not set — `foundry serve <project>` "
            "sets it; direct uvicorn users must export it",
            context={"missing_env": "FOUNDRY_SERVE_PROJECT"},
        )
    checkpoint = os.environ.get("FOUNDRY_CHECKPOINTER", "sqlite").strip()
    cors_raw = os.environ.get("FOUNDRY_CORS_ORIGINS", "").strip()
    return create_app(
        Path(project),
        checkpoint=checkpoint,
        cors_origins=(
            [o.strip() for o in cors_raw.split(",") if o.strip()]
            if cors_raw
            else None
        ),
        route_prefix=os.environ.get("FOUNDRY_ROUTE_PREFIX", "").strip(),
    )


__all__ = ["create_app", "create_app_from_env"]
