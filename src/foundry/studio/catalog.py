"""Catalog browse / show / files / promote / deprecate (docs/72 §
Catalog).

Reads delegate to ``foundry.catalog``; the promote route is the ONLY way
studio mutates the catalog tree (human-gated: the request's explicit
``confirm`` flag replaces the CLI's interactive prompt). Deprecate is the
metadata-only companion (flips ``deprecated`` in versions.json — the same
data the CLI-side deprecation fields carry), equally confirm-gated and
committed + audited.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Query, Request

from foundry.catalog.loader import catalog_entries, load_versions_metadata
from foundry.catalog.promote import promote_artifact
from foundry.config.refs import FoundryRoots
from foundry.core.errors import ConfigLoadError, ConfigValidationError
from foundry.studio.context import StudioContext
from foundry.studio.schemas import (
    CatalogArtifactDetail,
    CatalogEntryModel,
    CatalogFile,
    CatalogFiles,
    CatalogVersionModel,
    DeprecateRequest,
    DeprecateResponse,
    PromoteRequest,
    PromoteResponse,
)
from foundry.studio.security import studio_operator
from foundry.versioning.artifacts import (
    versions_metadata_path,
    write_versions_metadata,
)

_KIND_SUBDIR = {
    "tool": "tools",
    "tools": "tools",
    "connection": "connections",
    "connections": "connections",
    "retriever": "retrievers",
    "retrievers": "retrievers",
}


def _roots(ctx: StudioContext) -> FoundryRoots:
    catalog_roots = ctx.catalog_roots()
    if not catalog_roots:
        raise ConfigLoadError(
            "no catalog roots found; set FOUNDRY_CATALOG_ROOTS or add a "
            "catalog/ tree at the repo root",
            context={"repo_root": str(ctx.repo_root)},
        )
    return FoundryRoots(
        catalog_roots=catalog_roots, projects_root=ctx.projects_root
    )


def _find_artifact(
    ctx: StudioContext, kind: str, name: str
) -> tuple[Path, str]:
    subdir = _KIND_SUBDIR.get(kind)
    if subdir is None:
        raise ConfigLoadError(
            f"unknown catalog kind {kind!r} (tools / connections / "
            "retrievers)",
            context={"kind": kind, "not_found": True},
        )
    for root in ctx.catalog_roots():
        candidate = root / subdir / name
        if candidate.is_dir():
            return candidate, subdir
    raise ConfigLoadError(
        f"catalog artifact {kind}/{name} not found",
        context={"kind": kind, "name": name, "not_found": True},
    )


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.get("/catalog", response_model=list[CatalogEntryModel])
    def catalog_list(
        kind: str | None = Query(None),
    ) -> list[CatalogEntryModel]:
        entries = catalog_entries(_roots(ctx))
        singular = {"tools": "tool", "connections": "connection",
                    "retrievers": "retriever"}.get(kind or "", kind)
        return [
            CatalogEntryModel(
                name=entry.name,
                kind=entry.kind,
                versions=list(entry.versions),
                latest=entry.latest,
                root=entry.root,
            )
            for entry in entries
            if singular is None or entry.kind == singular
        ]

    @router.get(
        "/catalog/{kind}/{name}", response_model=CatalogArtifactDetail
    )
    def catalog_show(kind: str, name: str) -> CatalogArtifactDetail:
        artifact_dir, subdir = _find_artifact(ctx, kind, name)
        metadata = load_versions_metadata(
            versions_metadata_path(artifact_dir)
        )
        on_disk = sorted(
            (
                entry.name
                for entry in artifact_dir.iterdir()
                if entry.is_dir() and entry.name.startswith("v")
            ),
            key=lambda v: int(v[1:]) if v[1:].isdigit() else 0,
        )
        recorded = {meta.version: meta for meta in metadata.versions}
        versions = [
            CatalogVersionModel(
                version=version,
                created_at=(
                    recorded[version].created_at
                    if version in recorded
                    else None
                ),
                created_by=(
                    recorded[version].created_by if version in recorded else ""
                ),
                eval_score=(
                    recorded[version].eval_score if version in recorded else None
                ),
                eval_run_id=(
                    recorded[version].eval_run_id if version in recorded else None
                ),
                notes=recorded[version].notes if version in recorded else None,
                deprecated=(
                    recorded[version].deprecated
                    if version in recorded
                    else False
                ),
                deprecation_reason=(
                    recorded[version].deprecation_reason
                    if version in recorded
                    else None
                ),
                schema_change=(
                    recorded[version].schema_change
                    if version in recorded
                    else None
                ),
            )
            for version in on_disk
        ]
        return CatalogArtifactDetail(
            name=name, kind=subdir.rstrip("s"), versions=versions
        )

    @router.get(
        "/catalog/{kind}/{name}/{version}/files",
        response_model=CatalogFiles,
    )
    def catalog_files(kind: str, name: str, version: str) -> CatalogFiles:
        artifact_dir, subdir = _find_artifact(ctx, kind, name)
        version_dir = artifact_dir / version
        if not version_dir.is_dir():
            raise ConfigLoadError(
                f"version {version!r} of {kind}/{name} not found",
                context={"version": version, "not_found": True},
            )
        files = [
            CatalogFile(
                path=path.relative_to(version_dir).as_posix(),
                content=path.read_text(),
            )
            for path in sorted(version_dir.rglob("*"))
            if path.is_file() and "__pycache__" not in path.parts
        ]
        return CatalogFiles(
            ref=f"catalog/{subdir}/{name}", version=version, files=files
        )

    @router.post("/catalog/promote", response_model=PromoteResponse)
    def catalog_promote(body: PromoteRequest, request: Request) -> PromoteResponse:
        if not body.confirm:
            raise ConfigValidationError(
                "catalog promotion is human-gated: set confirm=true after "
                "reviewing the target (the UI confirmation replaces the "
                "interactive prompt — docs/72)",
                context={"target": body.target},
            )
        catalog_root = ctx.catalog_roots()
        if not catalog_root:
            raise ConfigLoadError(
                "no catalog root to promote into",
                context={"repo_root": str(ctx.repo_root)},
            )
        result = promote_artifact(
            body.target,
            projects_root=ctx.projects_root,
            catalog_root=catalog_root[0],
            backend=ctx.backend(),
            floor=body.floor,
            strict_semver=body.strict_semver,
            allow_breaking=body.allow_breaking,
            operator=studio_operator(),
            transport=ctx.transport,
            confirm=lambda _message: True,
            notes=body.notes,
        )
        return PromoteResponse(
            catalog_ref=result.catalog_ref,
            kind=result.kind,
            eval_score=result.eval_score,
            schema_change=result.schema_change,
            commit_sha=result.commit_sha,
        )

    @router.post("/catalog/deprecate", response_model=DeprecateResponse)
    def catalog_deprecate(body: DeprecateRequest) -> DeprecateResponse:
        if not body.confirm:
            raise ConfigValidationError(
                "catalog deprecation is human-gated: set confirm=true",
                context={"ref": body.ref},
            )
        kind, _, name = body.ref.rpartition("/")
        artifact_dir, subdir = _find_artifact(ctx, kind or "tool", name)
        metadata = load_versions_metadata(
            versions_metadata_path(artifact_dir)
        )
        target = metadata.get(body.version)
        if target is None:
            raise ConfigLoadError(
                f"version {body.version!r} of {body.ref} has no "
                "versions.json record to deprecate",
                context={"ref": body.ref, "version": body.version,
                         "not_found": True},
            )
        updated = target.model_copy(
            update={
                "deprecated": True,
                "deprecation_reason": body.reason,
            }
        )
        metadata_versions = [
            updated if meta.version == body.version else meta
            for meta in metadata.versions
        ]
        path = write_versions_metadata(
            artifact_dir,
            metadata.model_copy(update={"versions": metadata_versions}),
        )
        backend = ctx.backend()
        commit_sha = backend.commit(
            [path],
            f"studio(catalog): deprecate {subdir}/{name}@{body.version}",
        )
        # The commit IS the audit record here: catalog artifacts have no
        # project-side .foundry/ log, and writing one under catalog/ would
        # leave the shared tree dirty (promote's audit entry lands in the
        # SOURCE project's log for the same reason).
        return DeprecateResponse(
            ref=f"{subdir}/{name}",
            version=body.version,
            deprecated=True,
            commit_sha=commit_sha,
        )

    return router


__all__ = ["build_router"]
