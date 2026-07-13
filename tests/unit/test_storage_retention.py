"""Retention, pinning, archival + storage CLI executor coverage (docs/81).

Every test redirects FOUNDRY_HOME into tmp_path; run-dir ages are faked with
os.utime so the gc/archive cutoff math is exercised against real mtimes.
"""

from __future__ import annotations

import json
import os
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from foundry.core.errors import StorageError
from foundry.storage import (
    archive,
    archives_root,
    gc,
    list_pinned,
    parse_duration,
    pin,
    pinned_global_path,
    runs_root,
    unpin,
)
from foundry.storage.cli import (
    execute_archive,
    execute_gc,
    execute_list_pinned,
    execute_pin,
    execute_stats,
    execute_unpin,
)
from foundry.storage.retention import KindRetention, RetentionPolicy


@pytest.fixture(autouse=True)
def _foundry_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "fh"))


def _make_run(run_id: str, *, days_old: float) -> Path:
    run_dir = runs_root() / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "meta.json").write_text("{}")
    (run_dir / "trace.jsonl").write_text("event\n")
    stamp = (datetime.now(UTC) - timedelta(days=days_old)).timestamp()
    for path in (run_dir / "meta.json", run_dir / "trace.jsonl", run_dir):
        os.utime(path, (stamp, stamp))
    return run_dir


# --- parse_duration ------------------------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("7d", timedelta(days=7)),
        ("90d", timedelta(days=90)),
        ("24h", timedelta(hours=24)),
        (" 30d ", timedelta(days=30)),
    ],
)
def test_parse_duration_accepts_days_and_hours(text: str, expected: timedelta) -> None:
    assert parse_duration(text) == expected


@pytest.mark.unit
@pytest.mark.parametrize("text", ["", "7w", "d", "1.5d", "90", "h24"])
def test_parse_duration_refuses_bad_input(text: str) -> None:
    with pytest.raises(StorageError):
        parse_duration(text)


# --- retention policy ------------------------------------------------------------


@pytest.mark.unit
def test_retention_policy_defaults_match_docs_81() -> None:
    policy = RetentionPolicy()
    assert policy.runs == KindRetention(
        raw_days=90, summary_days=365, archive_after_days=90, delete_archives_after_days=1825
    )
    assert policy.eval_results.raw_days == 365
    loaded = RetentionPolicy.model_validate({"runs": {"raw_days": 7}})
    assert loaded.runs.raw_days == 7


# --- pinning ---------------------------------------------------------------------


@pytest.mark.unit
def test_pin_unpin_list_global() -> None:
    pin("run", "01RUNAAA", reason="incident-042 investigation")
    pin("run", "01RUNBBB")
    items = list_pinned()
    assert [(i.kind, i.id, i.reason, i.scope) for i in items] == [
        ("run", "01RUNAAA", "incident-042 investigation", "global"),
        ("run", "01RUNBBB", None, "global"),
    ]
    assert unpin("run", "01RUNAAA") is True
    assert unpin("run", "01RUNAAA") is False  # idempotent
    assert [i.id for i in list_pinned()] == ["01RUNBBB"]


@pytest.mark.unit
def test_pin_is_idempotent() -> None:
    pin("run", "01RUNAAA", reason="first")
    pin("run", "01RUNAAA", reason="second")
    items = list_pinned()
    assert len(items) == 1
    assert items[0].reason == "first"


@pytest.mark.unit
def test_project_pins_live_in_project_dir(tmp_path: Path) -> None:
    project_dir = tmp_path / "projects" / "example_corp"
    project_dir.mkdir(parents=True)
    pin("run", "01PROJRUN", project_dir=project_dir)
    assert (project_dir / ".foundry" / "pinned_runs.txt").is_file()
    assert list_pinned() == []  # global list unaffected
    items = list_pinned(project_dir=project_dir)
    assert [(i.id, i.scope) for i in items] == [("01PROJRUN", "project")]
    assert unpin("run", "01PROJRUN", project_dir=project_dir) is True


@pytest.mark.unit
def test_pin_file_parsing_tolerates_blanks_and_comments() -> None:
    path = pinned_global_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n# a full-line comment\nrun 01RUNAAA  # keep for audit\n\nrun 01RUNBBB\nmalformed\n"
    )
    items = list_pinned()
    assert [(i.id, i.reason) for i in items] == [
        ("01RUNAAA", "keep for audit"),
        ("01RUNBBB", None),
    ]


# --- gc --------------------------------------------------------------------------


@pytest.mark.unit
def test_gc_deletes_old_runs_and_keeps_recent() -> None:
    old = _make_run("01OLD", days_old=120)
    recent = _make_run("01RECENT", days_old=5)
    report = gc(older_than_days=90)
    assert report.candidates == ["01OLD"]
    assert report.deleted == ["01OLD"]
    assert report.skipped_pinned == [] and report.forced == []
    assert not old.exists() and recent.exists()


@pytest.mark.unit
def test_gc_dry_run_deletes_nothing() -> None:
    old = _make_run("01OLD", days_old=120)
    report = gc(older_than_days=90, dry_run=True)
    assert report.dry_run is True
    assert report.candidates == ["01OLD"]
    assert report.deleted == []
    assert old.exists()


@pytest.mark.unit
def test_gc_skips_pinned_runs() -> None:
    pinned_dir = _make_run("01PINNED", days_old=120)
    _make_run("01DOOMED", days_old=120)
    pin("run", "01PINNED", reason="legal hold")
    report = gc(older_than_days=90)
    assert report.skipped_pinned == ["01PINNED"]
    assert report.deleted == ["01DOOMED"]
    assert report.forced == []
    assert pinned_dir.exists()


@pytest.mark.unit
def test_gc_force_deletes_pinned_and_records_it() -> None:
    pinned_dir = _make_run("01PINNED", days_old=120)
    pin("run", "01PINNED")
    report = gc(older_than_days=90, force=True)
    assert report.deleted == ["01PINNED"]
    assert report.forced == ["01PINNED"]
    assert report.skipped_pinned == []
    assert not pinned_dir.exists()


@pytest.mark.unit
def test_gc_age_is_newest_file_mtime() -> None:
    """A dir whose newest file is fresh must not age out, whatever the dir mtime says."""
    run_dir = _make_run("01ACTIVE", days_old=120)
    fresh = run_dir / "outputs.json"
    fresh.write_text("{}")  # fresh mtime; dir utime already backdated
    report = gc(older_than_days=90)
    assert report.candidates == []
    assert run_dir.exists()


@pytest.mark.unit
def test_gc_unknown_kind_refused() -> None:
    with pytest.raises(StorageError, match="only 'runs'"):
        gc(kind="checkpoints", older_than_days=1)


@pytest.mark.unit
def test_gc_missing_runs_root_is_empty_report() -> None:
    report = gc(older_than_days=90)
    assert report.candidates == [] and report.deleted == []


# --- archive ---------------------------------------------------------------------


@pytest.mark.unit
def test_archive_compacts_old_runs_into_monthly_tarball() -> None:
    old = _make_run("01OLD", days_old=120)
    recent = _make_run("01RECENT", days_old=5)
    month = datetime.fromtimestamp(old.stat().st_mtime, tz=UTC).strftime("%Y-%m")
    report = archive(older_than_days=90)
    assert report.archived == ["01OLD"]
    expected = archives_root() / f"runs-{month}.tar.gz"
    assert report.archives == [str(expected)]
    assert not old.exists() and recent.exists()
    with tarfile.open(expected) as tar:
        names = tar.getnames()
    assert "01OLD/meta.json" in names and "01OLD/trace.jsonl" in names


@pytest.mark.unit
def test_archive_excludes_pinned_runs() -> None:
    pinned_dir = _make_run("01PINNED", days_old=120)
    pin("run", "01PINNED")
    report = archive(older_than_days=90)
    assert report.skipped_pinned == ["01PINNED"]
    assert report.archived == [] and report.archives == []
    assert pinned_dir.exists()


@pytest.mark.unit
def test_archive_never_rewrites_a_closed_month() -> None:
    old = _make_run("01OLD", days_old=120)
    month = datetime.fromtimestamp(old.stat().st_mtime, tz=UTC).strftime("%Y-%m")
    archives_root().mkdir(parents=True, exist_ok=True)
    existing = archives_root() / f"runs-{month}.tar.gz"
    existing.write_bytes(b"closed month; append-only")
    report = archive(older_than_days=90)
    assert report.archives == [str(archives_root() / f"runs-{month}.1.tar.gz")]
    assert existing.read_bytes() == b"closed month; append-only"


# --- CLI executors ---------------------------------------------------------------


@pytest.mark.unit
def test_execute_stats_reports_kinds(capsys: pytest.CaptureFixture[str]) -> None:
    _make_run("01RUN", days_old=1)
    assert execute_stats() == 0
    out = capsys.readouterr().out
    assert "runs" in out and "archives" in out and "observability.db" in out


@pytest.mark.unit
def test_execute_stats_json(capsys: pytest.CaptureFixture[str]) -> None:
    _make_run("01RUN", days_old=1)
    assert execute_stats(json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    runs_row = next(row for row in payload["kinds"] if row["kind"] == "runs")
    assert runs_row["items"] == 1 and runs_row["bytes"] > 0


@pytest.mark.unit
def test_execute_gc_happy_path(capsys: pytest.CaptureFixture[str]) -> None:
    _make_run("01OLD", days_old=120)
    assert execute_gc("runs", "90d", dry_run=False, force=False) == 0
    out = capsys.readouterr().out
    assert "01OLD" in out
    assert not (runs_root() / "01OLD").exists()


@pytest.mark.unit
def test_execute_gc_json_output(capsys: pytest.CaptureFixture[str]) -> None:
    _make_run("01OLD", days_old=120)
    assert execute_gc("runs", "90d", dry_run=True, force=False, json_output=True) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["candidates"] == ["01OLD"] and payload["dry_run"] is True


@pytest.mark.unit
def test_execute_gc_bad_duration_exits_2(capsys: pytest.CaptureFixture[str]) -> None:
    assert execute_gc("runs", "ninety days", dry_run=False, force=False) == 2
    assert "StorageError" in capsys.readouterr().err


@pytest.mark.unit
def test_execute_pin_unpin_list(capsys: pytest.CaptureFixture[str]) -> None:
    assert execute_pin("run", "01RUNAAA", reason="audit finding") == 0
    assert execute_list_pinned() == 0
    out = capsys.readouterr().out
    assert "01RUNAAA" in out and "audit finding" in out
    assert execute_unpin("run", "01RUNAAA") == 0
    assert execute_unpin("run", "01RUNAAA") == 0  # not pinned: still exit 0
    assert execute_list_pinned() == 0
    assert "(nothing pinned)" in capsys.readouterr().out


@pytest.mark.unit
def test_execute_archive(capsys: pytest.CaptureFixture[str]) -> None:
    _make_run("01OLD", days_old=120)
    assert execute_archive("runs", "90d") == 0
    assert "archived: 1" in capsys.readouterr().out
    assert execute_archive("runs", "bogus") == 2


# --- project-scoped pin warning (docs/81 § Pinned retention, scope caveat) -------


def _make_project_pin(root: Path, project: str, run_id: str) -> Path:
    pin_file = root / "projects" / project / ".foundry" / "pinned_runs.txt"
    pin_file.parent.mkdir(parents=True)
    pin_file.write_text(f"run {run_id}  # project-scoped\n")
    return pin_file


@pytest.mark.unit
def test_project_pin_files_finds_only_populated_files(tmp_path: Path) -> None:
    projects_root = tmp_path / "projects"
    pinned = _make_project_pin(tmp_path, "demo", "01PROJPIN")
    empty = tmp_path / "projects" / "other" / ".foundry" / "pinned_runs.txt"
    empty.parent.mkdir(parents=True)
    empty.write_text("# comments only\n\n")
    from foundry.storage import project_pin_files

    assert project_pin_files(projects_root) == [pinned]
    assert project_pin_files(tmp_path / "missing") == []


@pytest.mark.unit
def test_gc_warns_loudly_when_project_pins_exist_and_does_not_honour_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """gc honours GLOBAL pins only: a project-scoped pin does NOT protect
    its run, and the CLI must say so loudly before collecting."""
    monkeypatch.chdir(tmp_path)
    pin_file = _make_project_pin(tmp_path, "demo", "01PROJPIN")
    _make_run("01PROJPIN", days_old=120)

    assert execute_gc("runs", "90d", dry_run=False, force=False) == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err
    assert "GLOBAL pins only" in captured.err
    assert str(pin_file) in captured.err
    assert "foundry storage pin run" in captured.err
    # the documented behaviour: the project pin did not protect the run
    assert not (runs_root() / "01PROJPIN").exists()


@pytest.mark.unit
def test_archive_warns_when_project_pins_exist(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_project_pin(tmp_path, "demo", "01PROJPIN")
    _make_run("01OLD", days_old=120)
    assert execute_archive("runs", "90d") == 0
    captured = capsys.readouterr()
    assert "WARNING" in captured.err and "GLOBAL pins only" in captured.err
    assert "archived: 1" in captured.out


@pytest.mark.unit
def test_gc_does_not_warn_without_project_pins(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    _make_run("01OLD", days_old=120)
    assert execute_gc("runs", "90d", dry_run=True, force=False) == 0
    assert "WARNING" not in capsys.readouterr().err
