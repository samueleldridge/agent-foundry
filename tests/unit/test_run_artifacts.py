"""RunArtifactWriter unit coverage (Phase 4 pre-work).

``next_sequence`` drives resumed-run event numbering; a SIGKILL mid-write
leaves a torn trailing line in events.jsonl that must be truncated, not
counted (Phase 3 review finding 3).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from foundry.core import RunId, RunStarted
from foundry.observability.artifacts import RunArtifactWriter


def _writer(tmp_path: Path) -> RunArtifactWriter:
    return RunArtifactWriter(RunId.new(), directory=tmp_path / "run")


def _event(writer: RunArtifactWriter, sequence: int) -> RunStarted:
    return RunStarted(
        run_id=writer.run_id,
        sequence=sequence,
        timestamp=datetime.now(UTC),
        project="p",
        system_version="v",
        pin_set_hash="h",
        inputs_hash="i",
    )


@pytest.mark.unit
def test_next_sequence_counts_complete_lines(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    assert writer.next_sequence() == 0
    writer.record_event(_event(writer, 0))
    writer.record_event(_event(writer, 1))
    assert writer.next_sequence() == 2


@pytest.mark.unit
def test_next_sequence_empty_file_is_zero(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    (writer.directory / "events.jsonl").write_bytes(b"")
    assert writer.next_sequence() == 0


@pytest.mark.unit
def test_next_sequence_truncates_torn_trailing_line(tmp_path: Path) -> None:
    """SIGKILL mid-write: two complete events + a torn third line. The torn
    line must not count AND must be removed so the resumed process's next
    append starts on a clean line boundary."""
    writer = _writer(tmp_path)
    writer.record_event(_event(writer, 0))
    writer.record_event(_event(writer, 1))
    events_path = writer.directory / "events.jsonl"
    with events_path.open("ab") as fh:
        fh.write(b'{"event": "llm.comp')  # torn mid-write, no newline

    assert writer.next_sequence() == 2
    # the torn tail is gone; every remaining line parses
    lines = events_path.read_bytes().splitlines()
    assert len(lines) == 2
    for line in lines:
        json.loads(line)

    # a resumed writer appends cleanly after the truncation
    writer.record_event(_event(writer, 2))
    parsed = [json.loads(line) for line in events_path.read_text().splitlines()]
    assert [event["sequence"] for event in parsed] == [0, 1, 2]


@pytest.mark.unit
def test_next_sequence_only_a_torn_line_is_zero(tmp_path: Path) -> None:
    writer = _writer(tmp_path)
    events_path = writer.directory / "events.jsonl"
    events_path.write_bytes(b'{"torn": tru')
    assert writer.next_sequence() == 0
    assert events_path.read_bytes() == b""
