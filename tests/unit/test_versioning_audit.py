"""foundry.versioning.audit unit tests (docs/52 § Audit log format)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from foundry.core.errors import VersioningError
from foundry.versioning.audit import (
    Operator,
    append_audit_entry,
    audit_log_path,
    new_audit_entry,
    read_audit_entries,
    resolve_operator,
)

_HUMAN = Operator(kind="human", human_email="op@example.com")


def _entry(
    *, type_: str = "rollback", scope: str = "hello/tool", summary: str = "x"
) -> object:
    return new_audit_entry(
        type=type_,  # type: ignore[arg-type]
        scope=scope,
        summary=summary,
        operator=_HUMAN,
        commit_sha="abc123",
        files_affected=["projects/hello/system.yaml"],
    )


@pytest.mark.unit
def test_append_and_read_roundtrip(tmp_path: Path) -> None:
    entry = new_audit_entry(
        type="rollback",
        scope="hello/tool banner",
        summary="banner: v2 -> v1",
        operator=_HUMAN,
        commit_sha="abc123",
        files_affected=["projects/hello/system.yaml"],
        overrides_used=["working_tree_clean"],
    )
    path = append_audit_entry(tmp_path, entry)
    assert path == audit_log_path(tmp_path)
    entries = read_audit_entries(tmp_path)
    assert len(entries) == 1
    loaded = entries[0]
    assert loaded.id == entry.id and len(loaded.id) == 26  # ULID
    assert loaded.commit_sha == "abc123"
    assert loaded.operator.kind == "human"
    assert loaded.overrides_used == ["working_tree_clean"]


@pytest.mark.unit
def test_append_only_earlier_lines_never_rewritten(tmp_path: Path) -> None:
    append_audit_entry(tmp_path, _entry(summary="first"))  # type: ignore[arg-type]
    first_line = audit_log_path(tmp_path).read_text().splitlines()[0]
    append_audit_entry(tmp_path, _entry(summary="second"))  # type: ignore[arg-type]
    lines = audit_log_path(tmp_path).read_text().splitlines()
    assert lines[0] == first_line
    assert len(lines) == 2


@pytest.mark.unit
def test_filters_by_type_artifact_and_since(tmp_path: Path) -> None:
    append_audit_entry(
        tmp_path, _entry(type_="rollback", summary="pin banner v2 -> v1")  # type: ignore[arg-type]
    )
    append_audit_entry(
        tmp_path, _entry(type_="catalog", summary="promoted word_stats")  # type: ignore[arg-type]
    )
    assert len(read_audit_entries(tmp_path, type="rollback")) == 1
    assert len(read_audit_entries(tmp_path, artifact="word_stats")) == 1
    assert len(read_audit_entries(tmp_path, artifact="system.yaml")) == 2
    future = datetime.now(UTC) + timedelta(hours=1)
    assert read_audit_entries(tmp_path, since=future) == []


@pytest.mark.unit
def test_corrupt_line_raises_loudly(tmp_path: Path) -> None:
    append_audit_entry(tmp_path, _entry())  # type: ignore[arg-type]
    with audit_log_path(tmp_path).open("a") as handle:
        handle.write("{not json\n")
    with pytest.raises(VersioningError, match="corrupt audit entry"):
        read_audit_entries(tmp_path)


@pytest.mark.unit
def test_missing_log_reads_empty(tmp_path: Path) -> None:
    assert read_audit_entries(tmp_path) == []


@pytest.mark.unit
def test_lines_are_compact_single_json_objects(tmp_path: Path) -> None:
    append_audit_entry(tmp_path, _entry())  # type: ignore[arg-type]
    line = audit_log_path(tmp_path).read_text().splitlines()[0]
    parsed = json.loads(line)
    assert "cluster_id" not in parsed  # exclude_none keeps lines compact
    assert parsed["schema_version"] == 1


@pytest.mark.unit
def test_resolve_operator_identities(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("GITHUB_ACTOR", raising=False)
    human = resolve_operator(git_email="op@example.com")
    assert human.kind == "human" and human.human_email == "op@example.com"
    unknown = resolve_operator()
    assert unknown.kind == "human" and unknown.human_email == "unknown"
    meta = resolve_operator(git_email="sup@example.com", forge_run_id="01FORGE")
    assert meta.kind == "meta_agent"
    assert meta.forge_run_id == "01FORGE"
    assert meta.human_supervisor == "sup@example.com"
    monkeypatch.setenv("GITHUB_ACTOR", "ci-bot")
    ci = resolve_operator()
    assert ci.kind == "ci" and ci.human_email == "ci-bot"
