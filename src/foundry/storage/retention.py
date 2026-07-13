"""Retention: TTL garbage collection, monthly archival, pinning (docs/81).

Scope honesty for this codebase: run, eval, and forge artifacts all live
under ``runs_root()`` (eval artifacts are written to ``runs/<eval_run_id>``),
so ``gc``/``archive`` implement kind ``"runs"`` concretely and refuse other
kinds. ``RetentionPolicy`` still carries per-kind knobs so operator config
composes when the trees split.

Pin scoping: ``gc``/``archive`` consult the **global** pin file only
(``~/.foundry/pinned_global.txt``). Project pin files
(``<project>/.foundry/pinned_runs.txt``) are read by ``pin``/``unpin``/
``list_pinned`` when ``project_dir`` is given; wiring project pins into gc is
deliberately deferred (gc has no project context — run dirs carry no project
provenance the pin file could be resolved against). Instead, the storage CLI
warns LOUDLY before gc/archive when project pin files exist under the working
directory's ``projects/`` tree (see :func:`project_pin_files`): protect a run
from gc by pinning it globally (``foundry storage pin run <id>``).

A run directory's age is the mtime of its newest file (falling back to the
directory mtime when empty) — a run still being appended to never ages out.
All timestamps are timezone-aware UTC.
"""

from __future__ import annotations

import re
import shutil
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import structlog
from pydantic import BaseModel, ConfigDict

from foundry.core.errors import StorageError
from foundry.storage.paths import archives_root, pinned_global_path, runs_root

_log: structlog.typing.FilteringBoundLogger = structlog.get_logger("foundry.storage")

# Pin kinds that protect entries in the runs tree ("run" is the CLI's
# singular spelling; eval runs share the tree per the module docstring).
_RUNS_PIN_KINDS = frozenset({"run", "runs", "eval_run", "eval_results"})


# --- policy ------------------------------------------------------------------


class KindRetention(BaseModel):
    """Retention knobs for one artifact kind (docs/81 § Retention policy)."""

    model_config = ConfigDict(extra="forbid")

    raw_days: int = 90
    summary_days: int = 365
    archive_after_days: int = 90
    delete_archives_after_days: int = 1825


class RetentionPolicy(BaseModel):
    """Operator-tunable retention defaults; loadable from YAML-shaped dicts."""

    model_config = ConfigDict(extra="forbid")

    runs: KindRetention = KindRetention()
    eval_results: KindRetention = KindRetention(
        raw_days=365,
        summary_days=1825,
        archive_after_days=365,
        delete_archives_after_days=1825,
    )


# --- durations ---------------------------------------------------------------

_DURATION = re.compile(r"^\s*(\d+)\s*([dh])\s*$")


def parse_duration(text: str) -> timedelta:
    """Parse ``"90d"`` / ``"24h"`` into a timedelta; anything else refuses."""
    match = _DURATION.match(text)
    if match is None:
        raise StorageError(
            f"invalid duration {text!r}: expected an integer followed by "
            "'d' (days) or 'h' (hours), e.g. '90d' or '24h'",
            context={"duration": text},
        )
    value = int(match.group(1))
    return timedelta(days=value) if match.group(2) == "d" else timedelta(hours=value)


# --- pinning -----------------------------------------------------------------


class PinnedItem(BaseModel):
    """One pinned artifact, exempt from TTL deletion."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    id: str
    reason: str | None = None
    scope: str  # "global" or "project"


def _pin_file(project_dir: Path | None) -> Path:
    if project_dir is None:
        return pinned_global_path()
    return project_dir / ".foundry" / "pinned_runs.txt"


def _parse_pin_file(path: Path, scope: str) -> list[PinnedItem]:
    """Tolerant parse: blank lines and ``#`` comment lines are skipped."""
    if not path.is_file():
        return []
    items: list[PinnedItem] = []
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        body, _, comment = line.partition("#")
        fields = body.split()
        if len(fields) < 2:
            continue
        reason = comment.strip() or None
        items.append(PinnedItem(kind=fields[0], id=fields[1], reason=reason, scope=scope))
    return items


def pin(
    kind: str,
    id: str,
    *,
    reason: str | None = None,
    project_dir: Path | None = None,
) -> PinnedItem:
    """Mark ``<kind> <id>`` as exempt from TTL deletion. Idempotent."""
    scope = "global" if project_dir is None else "project"
    path = _pin_file(project_dir)
    for existing in _parse_pin_file(path, scope):
        if existing.kind == kind and existing.id == id:
            return existing
    path.parent.mkdir(parents=True, exist_ok=True)
    clean_reason = " ".join(reason.split()) if reason else None
    line = f"{kind} {id}" + (f"  # {clean_reason}" if clean_reason else "")
    with path.open("a") as handle:
        handle.write(line + "\n")
    return PinnedItem(kind=kind, id=id, reason=clean_reason, scope=scope)


def unpin(kind: str, id: str, project_dir: Path | None = None) -> bool:
    """Remove a pin; returns False when it was not pinned. Idempotent."""
    path = _pin_file(project_dir)
    if not path.is_file():
        return False
    kept: list[str] = []
    removed = False
    for raw_line in path.read_text().splitlines():
        line = raw_line.strip()
        fields = line.partition("#")[0].split()
        if len(fields) >= 2 and fields[0] == kind and fields[1] == id:
            removed = True
            continue
        kept.append(raw_line)
    if removed:
        path.write_text("".join(f"{line}\n" for line in kept))
    return removed


def list_pinned(project_dir: Path | None = None) -> list[PinnedItem]:
    """Global pins, plus the project's pins when ``project_dir`` is given."""
    items = _parse_pin_file(pinned_global_path(), "global")
    if project_dir is not None:
        items.extend(_parse_pin_file(_pin_file(project_dir), "project"))
    return items


def project_pin_files(projects_root: Path) -> list[Path]:
    """Project-scoped pin files under ``projects_root`` (repo convention:
    ``projects/<p>/.foundry/pinned_runs.txt``) that carry at least one pin.

    gc/archive do NOT honour these (module docstring: gc has no project
    context); the storage CLI uses this to warn loudly before collecting."""
    if not projects_root.is_dir():
        return []
    return [
        candidate
        for candidate in sorted(projects_root.glob("*/.foundry/pinned_runs.txt"))
        if _parse_pin_file(candidate, "project")
    ]


def _globally_pinned_run_ids() -> set[str]:
    return {
        item.id
        for item in _parse_pin_file(pinned_global_path(), "global")
        if item.kind in _RUNS_PIN_KINDS
    }


# --- gc + archive ------------------------------------------------------------


class GcReport(BaseModel):
    """What ``gc`` saw and did (docs/81 § Cleanup commands)."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    cutoff: datetime
    dry_run: bool
    candidates: list[str] = []
    deleted: list[str] = []
    skipped_pinned: list[str] = []
    forced: list[str] = []


class ArchiveReport(BaseModel):
    """What ``archive`` compacted (docs/81 § Archival pattern)."""

    model_config = ConfigDict(extra="forbid")

    kind: str
    cutoff: datetime
    archives: list[str] = []
    archived: list[str] = []
    skipped_pinned: list[str] = []


def _require_runs_kind(kind: str) -> None:
    if kind != "runs":
        raise StorageError(
            f"unsupported gc/archive kind {kind!r}: only 'runs' is implemented "
            "(eval and forge artifacts share the runs tree in this codebase)",
            context={"kind": kind},
        )


def _dir_age(path: Path) -> datetime:
    """mtime of the newest file inside ``path`` (dir mtime when empty)."""
    newest = max(
        (entry.stat().st_mtime for entry in path.rglob("*") if entry.is_file()),
        default=path.stat().st_mtime,
    )
    return datetime.fromtimestamp(newest, tz=UTC)


def _aged_run_dirs(cutoff: datetime) -> list[tuple[Path, datetime]]:
    root = runs_root()
    if not root.is_dir():
        return []
    aged: list[tuple[Path, datetime]] = []
    for path in sorted(entry for entry in root.iterdir() if entry.is_dir()):
        age = _dir_age(path)
        if age < cutoff:
            aged.append((path, age))
    return aged


def gc(
    kind: str = "runs",
    older_than_days: float = 90.0,
    *,
    dry_run: bool = False,
    force: bool = False,
    now: datetime | None = None,
) -> GcReport:
    """Delete run directories older than the cutoff, honouring pins.

    ``force=True`` deletes pinned items too; every forced deletion is logged
    loudly and recorded in ``report.forced``. ``dry_run=True`` fills
    ``candidates``/``skipped_pinned`` without deleting anything.
    """
    _require_runs_kind(kind)
    moment = now if now is not None else datetime.now(UTC)
    cutoff = moment - timedelta(days=older_than_days)
    pinned = _globally_pinned_run_ids()
    report = GcReport(kind=kind, cutoff=cutoff, dry_run=dry_run)
    for path, age in _aged_run_dirs(cutoff):
        run_id = path.name
        report.candidates.append(run_id)
        if run_id in pinned and not force:
            report.skipped_pinned.append(run_id)
            continue
        if dry_run:
            continue
        shutil.rmtree(path)
        report.deleted.append(run_id)
        if run_id in pinned:
            report.forced.append(run_id)
            _log.warning(
                "storage.gc.force_deleted_pinned",
                kind=kind,
                id=run_id,
                path=str(path),
                age=age.isoformat(),
                cutoff=cutoff.isoformat(),
            )
    return report


def _next_archive_path(kind: str, month: str) -> Path:
    """Closed monthly archives are append-only: never rewrite an existing
    tarball — take a ``.<n>`` suffix instead (docs/81 § Archival pattern)."""
    root = archives_root()
    candidate = root / f"{kind}-{month}.tar.gz"
    counter = 1
    while candidate.exists():
        candidate = root / f"{kind}-{month}.{counter}.tar.gz"
        counter += 1
    return candidate


def archive(
    kind: str = "runs",
    older_than_days: float = 90.0,
    *,
    now: datetime | None = None,
) -> ArchiveReport:
    """Compact run dirs older than the cutoff into monthly tarballs, grouped
    by the YYYY-MM of each dir's age, then delete the originals. Pinned items
    are excluded."""
    _require_runs_kind(kind)
    moment = now if now is not None else datetime.now(UTC)
    cutoff = moment - timedelta(days=older_than_days)
    pinned = _globally_pinned_run_ids()
    report = ArchiveReport(kind=kind, cutoff=cutoff)
    groups: dict[str, list[Path]] = {}
    for path, age in _aged_run_dirs(cutoff):
        if path.name in pinned:
            report.skipped_pinned.append(path.name)
            continue
        groups.setdefault(f"{age.year:04d}-{age.month:02d}", []).append(path)
    if groups:
        archives_root().mkdir(parents=True, exist_ok=True)
    for month, paths in sorted(groups.items()):
        target = _next_archive_path(kind, month)
        with tarfile.open(target, "w:gz") as tar:
            for path in paths:
                tar.add(path, arcname=path.name)
        for path in paths:
            shutil.rmtree(path)
            report.archived.append(path.name)
        report.archives.append(str(target))
        _log.info(
            "storage.archive.month_compacted",
            kind=kind,
            month=month,
            archive=str(target),
            count=len(paths),
        )
    return report


__all__ = [
    "ArchiveReport",
    "GcReport",
    "KindRetention",
    "PinnedItem",
    "RetentionPolicy",
    "archive",
    "gc",
    "list_pinned",
    "parse_duration",
    "pin",
    "project_pin_files",
    "unpin",
]
