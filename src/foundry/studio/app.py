"""Foundry Studio control-plane FastAPI factory (docs/72).

``create_studio_app(repo_root)`` assembles every route group under
``/api``, with:

- an app-lifespan **task group** all background work (chat runs, forge
  sessions, eval/test/deploy tasks) drives inside;
- the optional **bearer** gate on every ``/api/*`` route;
- ``X-Foundry-Studio-Version`` + ``X-Studio-Request-Id`` on every
  response and a ``foundry.studio.request`` span per request;
- structured **error handling** — every error body is a
  ``FoundryError.to_dict()`` envelope, never a stack trace; sandbox
  violations map to 403, missing resources to 404, rollback refusals to
  409;
- static-asset serving with an SPA history fallback, or the
  "frontend not built" placeholder page until Phase 10b ships the React
  tree.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from foundry.api.errors import (
    headers_for,
    status_for,
    validation_error_body,
)
from foundry.core.errors import (
    FoundryError,
    ProjectUnavailableError,
    RollbackError,
    SandboxViolation,
)
from foundry.observability.tracing import configure_observability, foundry_span
from foundry.studio import (
    catalog,
    chat,
    configs,
    connections,
    deploy,
    doctor,
    evals,
    forge,
    graph,
    layouts,
    obs,
    projects,
    providers,
    runs,
    storage,
    tasks,
    testing,
    versions,
)
from foundry.studio.context import STUDIO_VERSION, StudioContext
from foundry.studio.schemas import StudioHealth
from foundry.studio.security import make_auth_dependency

_PLACEHOLDER_HTML = """<!doctype html>
<html>
  <head><title>Foundry Studio</title></head>
  <body style="font-family: system-ui, sans-serif; max-width: 40rem;
               margin: 4rem auto; line-height: 1.5;">
    <h1>Foundry Studio</h1>
    <p><strong>The control-plane API is running.</strong></p>
    <p>The Studio frontend is not built yet (it ships in Phase 10b).
       Until then:</p>
    <ul>
      <li>API docs: <a href="/api/docs">/api/docs</a></li>
      <li>OpenAPI schema: <a href="/api/openapi.json">/api/openapi.json</a></li>
      <li>Health: <a href="/api/health">/api/health</a></li>
    </ul>
    <p>When the frontend exists (the separate
       <code>agent-foundry-studio</code> repository), build it with
       <code>npm run build</code> there and point
       <code>FOUNDRY_STUDIO_DIST</code> at its <code>dist/</code>
       (a sibling checkout's <code>dist/</code> is found automatically),
       then restart <code>foundry studio</code>.</p>
  </body>
</html>
"""


def resolve_assets_dir(repo_root: Path) -> Path | None:
    """Built-frontend resolution order (docs/72 § Packaging; the frontend
    lives in a SEPARATE repository):

    1. ``FOUNDRY_STUDIO_DIST`` — absolute path to the built ``dist/``.
       AUTHORITATIVE when set: if it doesn't hold a build, the placeholder
       is served rather than silently falling back to another checkout's
       assets (explicit config never gets shadowed; tests rely on this to
       isolate resolution).
    2. packaged assets under ``foundry/studio/_assets/`` (release wheels);
    3. the sibling frontend checkout's build,
       ``<repo_root>/../agent-foundry-studio/dist`` (dev default).

    None = no build found → serve the "frontend not built" placeholder.
    """
    import os

    def _holds_build(candidate: Path) -> bool:
        return candidate.is_dir() and (candidate / "index.html").is_file()

    override = os.environ.get("FOUNDRY_STUDIO_DIST", "").strip()
    if override:
        candidate = Path(override)
        return candidate if _holds_build(candidate) else None
    for candidate in (
        Path(__file__).parent / "_assets",
        repo_root.parent / "agent-foundry-studio" / "dist",
    ):
        if _holds_build(candidate):
            return candidate
    return None


def _studio_status_for(exc: FoundryError) -> int:
    if isinstance(exc, SandboxViolation):
        return 403
    if isinstance(exc, RollbackError):
        return 409
    if isinstance(exc, ProjectUnavailableError):
        # Failed dependency: the project needs env vars this process
        # doesn't have. The envelope carries env_vars + remedy.
        return 424
    if exc.context.get("not_found"):
        return 404
    return status_for(exc)


def create_studio_app(
    repo_root: Path | str | None = None,
    *,
    auth_token: str | None = None,
    checkpoint: str = "sqlite",
    transport: httpx.AsyncBaseTransport | None = None,
    serve_assets: bool = True,
) -> FastAPI:
    """Build the studio control-plane app rooted at ``repo_root`` (default:
    the current working directory, like the CLI)."""
    configure_observability()
    ctx = StudioContext(
        repo_root=Path(repo_root or Path.cwd()).resolve(),
        auth_token=auth_token,
        checkpoint=checkpoint,
        transport=transport,
    )
    ctx.chat = chat.ChatRegistry(ctx)
    ctx.forge = forge.ForgeSupervisor(ctx)
    ctx.tasks = tasks.TaskRegistry(ctx)
    # Studio-stored provider keys (docs/72 § Provider panel): loaded at
    # startup into os.environ ONLY where the var isn't already set — the
    # real environment always wins, mirroring the CLI .env loader.
    providers.apply_stored_credentials(ctx)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with anyio.create_task_group() as tg:
            ctx.bind(tg)
            try:
                yield
            finally:
                assert ctx.chat is not None
                for manager in ctx.chat.managers():
                    await manager.shutdown()
                tg.cancel_scope.cancel()

    app = FastAPI(
        title="foundry studio",
        version=STUDIO_VERSION,
        description=(
            "Foundry Studio control plane — every foundry CLI feature "
            "behind a dedicated API (docs/72)."
        ),
        lifespan=lifespan,
        openapi_url="/api/openapi.json",
        docs_url="/api/docs",
        redoc_url=None,
    )
    app.state.studio_context = ctx

    auth = make_auth_dependency(auth_token)
    from fastapi import APIRouter, Depends
    from fastapi.routing import APIRoute

    api = APIRouter(dependencies=[Depends(auth)])
    route_table: set[tuple[str, str]] = set()

    def _collect(router: APIRouter) -> APIRouter:
        for route in router.routes:
            if isinstance(route, APIRoute):
                for method in route.methods or set():
                    route_table.add((method, f"/api{route.path}"))
        return router

    @api.get("/health", response_model=StudioHealth)
    def health() -> StudioHealth:
        from foundry.configurator import forge_max_iter_default

        assert ctx.chat is not None and ctx.forge is not None
        return StudioHealth(
            status="ok",
            version=ctx.version,
            uptime_s=round(ctx.uptime_s(), 3),
            active_forge_runs=ctx.forge.active_count(),
            active_chat_sessions=ctx.chat.active_sessions(),
            run_manager_pool=ctx.chat.pool_size(),
            forge_max_iter_default=forge_max_iter_default(),
        )

    for module in (
        projects,
        providers,
        configs,
        catalog,
        doctor,
        obs,
        storage,
        runs,
        evals,
        versions,
        connections,
        forge,
        chat,
        graph,
        deploy,
        testing,
        layouts,
        tasks,
    ):
        api.include_router(_collect(module.build_router(ctx)))

    _collect(api)  # picks up /health (declared on the api router itself)
    app.include_router(api, prefix="/api")
    # The as-assembled route table — the contract tests' enumeration
    # source (FastAPI's include_router flattens lazily, so walking
    # app.routes no longer yields APIRoutes directly).
    app.state.api_route_table = frozenset(route_table)

    # --- observability middleware ---------------------------------------------------

    @app.middleware("http")
    async def studio_request(request: Request, call_next: Any) -> Response:
        request_id = (
            request.headers.get("X-Studio-Request-Id") or uuid.uuid4().hex
        )
        request.state.studio_request_id = request_id
        started = time.monotonic()
        with foundry_span(
            "foundry.studio.request",
            {
                "studio_request_id": request_id,
                "http.route": request.url.path,
                "http.method": request.method,
            },
        ) as span:
            response: Response = await call_next(request)
            span.set_attribute("http.status_code", response.status_code)
            span.set_attribute(
                "duration_ms", int((time.monotonic() - started) * 1000)
            )
        response.headers.setdefault("X-Studio-Request-Id", request_id)
        response.headers.setdefault("X-Foundry-Studio-Version", ctx.version)
        return response

    # --- error envelopes (docs/70 § Failure modes) ----------------------------------

    @app.exception_handler(FoundryError)
    async def foundry_error_handler(
        request: Request, exc: FoundryError
    ) -> JSONResponse:
        status = _studio_status_for(exc)
        return JSONResponse(
            status_code=status,
            content=exc.to_dict(),
            headers=headers_for(status),
        )

    @app.exception_handler(RequestValidationError)
    async def validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=400,
            content=validation_error_body([dict(e) for e in exc.errors()]),
        )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        content = (
            exc.detail
            if isinstance(exc.detail, dict)
            else {"error": str(exc.detail)}
        )
        return JSONResponse(status_code=exc.status_code, content=content)

    # --- static assets / placeholder ------------------------------------------------

    if serve_assets:
        assets_dir = resolve_assets_dir(ctx.repo_root)

        @app.get("/{path:path}", include_in_schema=False)
        async def spa(path: str) -> Response:
            if path == "api" or path.startswith("api/"):
                return JSONResponse(
                    status_code=404,
                    content={
                        "error_class": "NotFound",
                        "message": f"no such API route: /{path}",
                        "context": {"path": f"/{path}"},
                    },
                )
            if assets_dir is None:
                return HTMLResponse(_PLACEHOLDER_HTML)
            candidate = (assets_dir / path).resolve()
            if (
                path
                and candidate.is_relative_to(assets_dir.resolve())
                and candidate.is_file()
            ):
                return FileResponse(candidate)
            # SPA history fallback: any non-/api 404 → index.html.
            return FileResponse(assets_dir / "index.html")

    return app


__all__ = ["create_studio_app", "resolve_assets_dir"]
