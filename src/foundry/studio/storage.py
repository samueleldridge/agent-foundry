"""Storage stats / gc / archive / pins (docs/72 § Observability table —
storage rows). Delegates to ``foundry.storage.retention``; gc and archive
default to dry-run (dry-run first-class)."""

from __future__ import annotations

from fastapi import APIRouter

from foundry.core.errors import ConfigValidationError
from foundry.storage.cli import _tree_stats
from foundry.storage.paths import (
    archives_root,
    foundry_home,
    observability_db_path,
    runs_root,
)
from foundry.storage.retention import (
    archive,
    gc,
    list_pinned,
    parse_duration,
    pin,
    unpin,
)
from foundry.studio.context import StudioContext
from foundry.studio.schemas import (
    ArchiveReportModel,
    ArchiveRequest,
    GcReportModel,
    GcRequest,
    PinnedItemModel,
    PinRequest,
    StorageStats,
)


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.get("/storage/stats", response_model=StorageStats)
    def storage_stats() -> StorageStats:
        run_count, run_bytes = _tree_stats(runs_root())
        archive_count, archive_bytes = _tree_stats(archives_root())
        obs_path = observability_db_path()
        obs_bytes = obs_path.stat().st_size if obs_path.is_file() else 0
        return StorageStats(
            foundry_home=str(foundry_home()),
            kinds=[
                {"kind": "runs", "items": run_count, "bytes": run_bytes},
                {
                    "kind": "archives",
                    "items": archive_count,
                    "bytes": archive_bytes,
                },
                {
                    "kind": "observability.db",
                    "items": 1 if obs_bytes else 0,
                    "bytes": obs_bytes,
                },
            ],
        )

    @router.post("/storage/gc", response_model=GcReportModel)
    def storage_gc(body: GcRequest) -> GcReportModel:
        days = parse_duration(body.older_than).total_seconds() / 86_400
        report = gc(
            body.kind, days, dry_run=body.dry_run, force=body.force
        )
        return GcReportModel(
            kind=report.kind,
            dry_run=report.dry_run,
            candidates=list(report.candidates),
            deleted=list(report.deleted),
            skipped_pinned=list(report.skipped_pinned),
            forced=bool(report.forced),
        )

    @router.post("/storage/archive", response_model=ArchiveReportModel)
    def storage_archive(body: ArchiveRequest) -> ArchiveReportModel:
        days = parse_duration(body.older_than).total_seconds() / 86_400
        if body.dry_run:
            # Archive has no dry-run in the retention module; preview via
            # the gc candidate scan (same cutoff, nothing touched).
            preview = gc(body.kind, days, dry_run=True)
            return ArchiveReportModel(
                kind=body.kind,
                dry_run=True,
                archives=[],
                archived=list(preview.candidates),
                skipped_pinned=list(preview.skipped_pinned),
            )
        report = archive(body.kind, days)
        return ArchiveReportModel(
            kind=report.kind,
            dry_run=False,
            archives=[str(path) for path in report.archives],
            archived=list(report.archived),
            skipped_pinned=list(report.skipped_pinned),
        )

    @router.get("/storage/pins", response_model=list[PinnedItemModel])
    def storage_pins() -> list[PinnedItemModel]:
        return [
            PinnedItemModel(
                kind=item.kind,
                id=item.id,
                reason=item.reason,
                scope=item.scope,
            )
            for item in list_pinned()
        ]

    @router.post(
        "/storage/pins", response_model=PinnedItemModel, status_code=201
    )
    def storage_pin(body: PinRequest) -> PinnedItemModel:
        item = pin(body.kind, body.artifact_id, reason=body.reason)
        return PinnedItemModel(
            kind=item.kind, id=item.id, reason=item.reason, scope=item.scope
        )

    @router.delete("/storage/pins")
    def storage_unpin(kind: str = "run", artifact_id: str = "") -> dict[str, bool]:
        if not artifact_id:
            raise ConfigValidationError(
                "artifact_id query parameter is required",
                context={"kind": kind},
            )
        return {"removed": unpin(kind, artifact_id)}

    return router


__all__ = ["build_router"]
