"""Connections list / describe / health / refresh (docs/72 § Connections).

Descriptors serve ``redacted_config`` ONLY (docs/72 redaction rule 1);
credential material never leaves the process.
"""

from __future__ import annotations

from fastapi import APIRouter

from foundry.config import EnvSecretsProvider
from foundry.config.loader import load_project
from foundry.config.refs import FoundryRoots
from foundry.connections.health import run_connection_health
from foundry.connections.registry import PreparedConnection, prepare_connections
from foundry.core.errors import ConfigLoadError
from foundry.studio.context import StudioContext
from foundry.studio.schemas import (
    ConnectionHealthResponse,
    ConnectionInfo,
    HealthCaseModel,
)


def _prepared(
    ctx: StudioContext, project: str
) -> dict[str, PreparedConnection]:
    project_dir = ctx.project_dir(project)
    loaded = load_project(project_dir)
    return prepare_connections(
        loaded.system,
        FoundryRoots.for_project(project_dir),
        EnvSecretsProvider(),
        system_file=project_dir / "system.yaml",
    )


def _info(name: str, prepared: PreparedConnection) -> ConnectionInfo:
    descriptor = prepared.descriptor
    return ConnectionInfo(
        name=name,
        ref=prepared.ref.name if hasattr(prepared.ref, "name") else str(prepared.ref),
        version=descriptor.ref.rpartition("@")[2] or "",
        auth_scheme=str(descriptor.auth_scheme.value),
        principal=descriptor.principal,
        redacted_config=dict(descriptor.redacted_config),
    )


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.get(
        "/projects/{name}/connections",
        response_model=list[ConnectionInfo],
    )
    def list_connections(name: str) -> list[ConnectionInfo]:
        return [
            _info(conn_name, prepared)
            for conn_name, prepared in sorted(_prepared(ctx, name).items())
        ]

    @router.get(
        "/projects/{name}/connections/{conn}",
        response_model=ConnectionInfo,
    )
    def describe_connection(name: str, conn: str) -> ConnectionInfo:
        prepared = _prepared(ctx, name)
        if conn not in prepared:
            raise ConfigLoadError(
                f"connection {conn!r} is not bound in project {name!r}",
                context={"connection": conn, "not_found": True},
            )
        return _info(conn, prepared[conn])

    @router.post(
        "/projects/{name}/connections/{conn}/health",
        response_model=ConnectionHealthResponse,
    )
    async def connection_health(
        name: str, conn: str
    ) -> ConnectionHealthResponse:
        prepared = _prepared(ctx, name)
        if conn not in prepared:
            raise ConfigLoadError(
                f"connection {conn!r} is not bound in project {name!r}",
                context={"connection": conn, "not_found": True},
            )
        report = await run_connection_health(
            prepared[conn], project=name, http_transport=ctx.transport
        )
        return ConnectionHealthResponse(
            connection=report.connection,
            ref=report.ref,
            ok=report.ok,
            checked_at=report.checked_at,
            cases=[
                HealthCaseModel(
                    case_id=case.case_id,
                    ok=case.ok,
                    latency_ms=case.latency_ms,
                    message=case.message,
                )
                for case in report.cases
            ],
        )

    @router.post("/projects/{name}/connections/{conn}/refresh")
    def refresh_connection(name: str, conn: str) -> dict[str, bool]:
        # The studio holds no long-lived pools of its own; refresh =
        # recompile so the chat manager's next run re-prepares the binding.
        prepared = _prepared(ctx, name)
        if conn not in prepared:
            raise ConfigLoadError(
                f"connection {conn!r} is not bound in project {name!r}",
                context={"connection": conn, "not_found": True},
            )
        ctx.invalidate(name)
        return {"refreshed": True}

    return router


__all__ = ["build_router"]
