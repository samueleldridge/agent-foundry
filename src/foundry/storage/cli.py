"""Plain executor functions for ``foundry storage <subcommand>`` (docs/81).

The typer wiring lives in ``foundry.cli``; these executors take plain values
and return process exit codes (0 ok, 2 on StorageError) so they stay testable
without a CLI runner. Error rendering mirrors
``foundry.cli._helpers.print_foundry_error`` — importing ``foundry.cli`` from
here would invert the dependency direction, so the format is reproduced.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from foundry.core.errors import FoundryError, StorageError
from foundry.storage.paths import archives_root, observability_db_path, runs_root
from foundry.storage.retention import (
    ArchiveReport,
    GcReport,
    archive,
    gc,
    list_pinned,
    parse_duration,
    pin,
    project_pin_files,
    unpin,
)


def _print_error(exc: FoundryError) -> None:
    """Same shape as foundry.cli._helpers.print_foundry_error."""
    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    for key, value in exc.context.items():
        if value is None or f"{key}:" in str(exc):
            continue
        print(f"  {key}: {value}", file=sys.stderr)


def _fmt_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{int(value)} B"  # unreachable; keeps mypy satisfied


def _tree_stats(root: Path) -> tuple[int, int]:
    """(item_count, total_bytes) — items are top-level entries under root."""
    if not root.is_dir():
        return 0, 0
    items = 0
    total = 0
    for entry in sorted(root.iterdir()):
        items += 1
        if entry.is_file():
            total += entry.stat().st_size
        else:
            total += sum(f.stat().st_size for f in entry.rglob("*") if f.is_file())
    return items, total


# --- stats -------------------------------------------------------------------


def execute_stats(json_output: bool = False) -> int:
    """Disk usage by kind: runs, archives, observability db."""
    run_count, run_bytes = _tree_stats(runs_root())
    archive_count, archive_bytes = _tree_stats(archives_root())
    obs_path = observability_db_path()
    obs_bytes = obs_path.stat().st_size if obs_path.is_file() else 0
    rows: list[dict[str, Any]] = [
        {"kind": "runs", "items": run_count, "bytes": run_bytes},
        {"kind": "archives", "items": archive_count, "bytes": archive_bytes},
        {
            "kind": "observability.db",
            "items": 1 if obs_bytes else 0,
            "bytes": obs_bytes,
        },
    ]
    if json_output:
        print(json.dumps({"kinds": rows}, indent=2))
        return 0
    print(f"{'kind':<20} {'items':>8} {'size':>12}")
    for row in rows:
        print(f"{row['kind']:<20} {row['items']:>8} {_fmt_bytes(int(row['bytes'])):>12}")
    return 0


def _warn_project_pins(command: str) -> None:
    """gc/archive honour GLOBAL pins only (docs/81; retention module
    docstring). When project-scoped pin files exist under ``./projects/``,
    say so loudly BEFORE collecting — those pins will not protect anything."""
    files = project_pin_files(Path.cwd() / "projects")
    if not files:
        return
    print(
        f"WARNING: `foundry storage {command}` honours GLOBAL pins only "
        "(~/.foundry/pinned_global.txt). Project-scoped pin files exist and "
        "will NOT protect their runs:",
        file=sys.stderr,
    )
    for path in files:
        print(f"  {path}", file=sys.stderr)
    print(
        "  protect a run globally with: foundry storage pin run <run_id>",
        file=sys.stderr,
    )


# --- gc ----------------------------------------------------------------------


def _print_gc_report(report: GcReport) -> None:
    verb = "would delete" if report.dry_run else "deleted"
    print(
        f"gc kind={report.kind} cutoff={report.cutoff.isoformat()}"
        f"{' (dry run)' if report.dry_run else ''}"
    )
    print(f"  candidates:     {len(report.candidates)}")
    for run_id in report.candidates:
        marker = " [pinned]" if run_id in report.skipped_pinned else ""
        print(f"    {run_id}{marker}")
    if report.dry_run:
        count = len(report.candidates) - len(report.skipped_pinned)
    else:
        count = len(report.deleted)
    print(f"  {verb}: {count}")
    print(f"  skipped pinned: {len(report.skipped_pinned)}")
    if report.forced:
        print(f"  FORCED (pinned, deleted anyway): {', '.join(report.forced)}")


def execute_gc(
    kind: str,
    older_than: str,
    *,
    dry_run: bool,
    force: bool,
    json_output: bool = False,
) -> int:
    """``foundry storage gc --kind runs --older-than 90d [--dry-run] [--force]``."""
    _warn_project_pins("gc")
    try:
        delta = parse_duration(older_than)
        report = gc(
            kind=kind,
            older_than_days=delta.total_seconds() / 86400,
            dry_run=dry_run,
            force=force,
        )
    except StorageError as exc:
        _print_error(exc)
        return 2
    if json_output:
        print(report.model_dump_json(indent=2))
    else:
        _print_gc_report(report)
    return 0


# --- pinning -----------------------------------------------------------------


def execute_pin(kind: str, id: str, *, reason: str | None = None) -> int:
    try:
        item = pin(kind, id, reason=reason)
    except StorageError as exc:
        _print_error(exc)
        return 2
    suffix = f" ({item.reason})" if item.reason else ""
    print(f"pinned {item.kind} {item.id}{suffix}")
    return 0


def execute_unpin(kind: str, id: str) -> int:
    try:
        removed = unpin(kind, id)
    except StorageError as exc:
        _print_error(exc)
        return 2
    print(f"unpinned {kind} {id}" if removed else f"{kind} {id} was not pinned")
    return 0


def execute_list_pinned(json_output: bool = False) -> int:
    try:
        items = list_pinned()
    except StorageError as exc:
        _print_error(exc)
        return 2
    if json_output:
        print(json.dumps({"pinned": [item.model_dump() for item in items]}, indent=2))
        return 0
    if not items:
        print("(nothing pinned)")
        return 0
    print(f"{'kind':<12} {'id':<32} reason")
    for item in items:
        print(f"{item.kind:<12} {item.id:<32} {item.reason or '-'}")
    return 0


# --- archive -----------------------------------------------------------------


def _print_archive_report(report: ArchiveReport) -> None:
    print(f"archive kind={report.kind} cutoff={report.cutoff.isoformat()}")
    print(f"  archived: {len(report.archived)}")
    for path in report.archives:
        print(f"    -> {path}")
    if report.skipped_pinned:
        print(f"  skipped pinned: {', '.join(report.skipped_pinned)}")


def execute_archive(kind: str, older_than: str) -> int:
    """``foundry storage archive --kind runs --older-than 90d``."""
    _warn_project_pins("archive")
    try:
        delta = parse_duration(older_than)
        report = archive(kind=kind, older_than_days=delta.total_seconds() / 86400)
    except StorageError as exc:
        _print_error(exc)
        return 2
    _print_archive_report(report)
    return 0


__all__ = [
    "execute_archive",
    "execute_gc",
    "execute_list_pinned",
    "execute_pin",
    "execute_stats",
    "execute_unpin",
]
