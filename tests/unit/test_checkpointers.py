"""Checkpointers (docs/03 § Phase 3): the langgraph-free SQLite store, the
saver bridge, and cross-process resume semantics on a real StateGraph."""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from foundry.core import FoundryMessage, MessageRole, TextBlock
from foundry.core.errors import CheckpointSchemaError, CompileError
from foundry.runtime._langgraph_types import (
    FoundrySqliteSaver,
    build_checkpointer,
)
from foundry.runtime.checkpointers import (
    SqliteCheckpointStore,
    default_checkpoint_db,
    graph_schema_fingerprint,
)

# --- store roundtrip -------------------------------------------------------------


@pytest.mark.unit
def test_sqlite_store_roundtrips_rows_across_connections(tmp_path: Path) -> None:
    db = tmp_path / "cp.sqlite"
    store = SqliteCheckpointStore(db)
    store.save_checkpoint(
        "t1", "", "c1", None, ("json", b"{}"), ("json", b"m"),
        blobs=[("state", "v1", ("msgpack", b"\x01"))],
    )
    store.save_checkpoint("t1", "", "c2", "c1", ("json", b"{2}"), ("json", b"m2"))
    store.save_write("t1", "", "c1", "task", 0, "state", ("json", b"1"), "p")
    store.close()

    reopened = SqliteCheckpointStore(db)
    checkpoints = reopened.load_checkpoints()
    assert ("t1", "", "c1", None, ("json", b"{}"), ("json", b"m")) in checkpoints
    assert ("t1", "", "c2", "c1", ("json", b"{2}"), ("json", b"m2")) in checkpoints
    assert reopened.load_writes() == [
        ("t1", "", "c1", "task", 0, "state", ("json", b"1"), "p")
    ]
    assert reopened.load_blobs() == [("t1", "", "state", "v1", ("msgpack", b"\x01"))]

    reopened.delete_thread("t1")
    assert reopened.load_checkpoints() == []
    assert reopened.load_writes() == []
    assert reopened.load_blobs() == []
    reopened.close()


@pytest.mark.unit
def test_checkpoint_and_blobs_mirror_atomically(tmp_path: Path) -> None:
    """A crash mid-mirror (simulated by a blob iterable that raises after
    the checkpoint row + first blob were staged) must persist NOTHING —
    otherwise a fresh process would rehydrate a silently-partial checkpoint
    (Phase 3 review finding 1)."""
    store = SqliteCheckpointStore(tmp_path / "cp.sqlite")

    class _ExplodingBlobs:
        def __iter__(self) -> Any:
            yield ("state", "v1", ("msgpack", b"\x01"))
            raise RuntimeError("simulated kill between blob writes")

    with pytest.raises(RuntimeError, match="simulated kill"):
        store.save_checkpoint(
            "t1", "", "c1", None, ("json", b"{}"), ("json", b"m"),
            blobs=_ExplodingBlobs(),  # type: ignore[arg-type]
        )
    # all-or-nothing: neither the checkpoint row nor the first blob survive
    assert store.load_checkpoints() == []
    assert store.load_blobs() == []

    # and the connection is still usable for the next (complete) transaction
    store.save_checkpoint(
        "t1", "", "c1", None, ("json", b"{}"), ("json", b"m"),
        blobs=[("state", "v1", ("msgpack", b"\x01"))],
    )
    assert len(store.load_checkpoints()) == 1
    assert store.load_blobs() == [("t1", "", "state", "v1", ("msgpack", b"\x01"))]
    store.close()


@pytest.mark.unit
def test_save_is_idempotent_per_primary_key(tmp_path: Path) -> None:
    store = SqliteCheckpointStore(tmp_path / "cp.sqlite")
    store.save_write("t", "", "c", "task", 0, "state", ("json", b"1"), "")
    store.save_write("t", "", "c", "task", 0, "state", ("json", b"2"), "")
    assert store.load_writes() == [("t", "", "c", "task", 0, "state", ("json", b"2"), "")]
    store.close()


# --- schema fingerprint (Phase 7 review finding 4) -----------------------------------


@pytest.mark.unit
def test_schema_fingerprint_mismatch_fails_loudly(tmp_path: Path) -> None:
    """Write a checkpoint under fingerprint A; reopening the store under
    fingerprint B (a different graph channel set, e.g. Phase 3's ``conv``
    vs Phase 7's ``conv__<agent>``) must raise a structured error instead
    of silently resuming with fresh channels."""
    db = tmp_path / "cp.sqlite"
    fp_a = graph_schema_fingerprint(["state", "output", "conv"])
    fp_b = graph_schema_fingerprint(["state", "output", "conv__qa_agent"])
    assert fp_a != fp_b

    store = SqliteCheckpointStore(db, fp_a)
    store.save_checkpoint("t1", "", "c1", None, ("json", b"{}"), ("json", b"m"))
    store.close()

    with pytest.raises(CheckpointSchemaError) as excinfo:
        SqliteCheckpointStore(db, fp_b)
    message = str(excinfo.value)
    assert "predates the current graph schema" in message
    assert "--run-id" in message  # remediation: rerun fresh or clear
    assert excinfo.value.context["stored_fingerprint"] == fp_a
    assert excinfo.value.context["current_fingerprint"] == fp_b


@pytest.mark.unit
def test_schema_fingerprint_match_resumes(tmp_path: Path) -> None:
    db = tmp_path / "cp.sqlite"
    fp = graph_schema_fingerprint(["state", "output", "conv__qa_agent"])
    store = SqliteCheckpointStore(db, fp)
    store.save_checkpoint("t1", "", "c1", None, ("json", b"{}"), ("json", b"m"))
    store.close()
    reopened = SqliteCheckpointStore(db, fp)  # same schema: no complaint
    assert len(reopened.load_checkpoints()) == 1
    reopened.close()


@pytest.mark.unit
def test_pre_fingerprint_checkpoints_fail_loudly(tmp_path: Path) -> None:
    """A legacy database (checkpoints written before fingerprinting, e.g.
    Phase 3) carries no stamp; opening it under the current schema must
    also fail loudly — that is the exact conv → conv__<agent> silent-resume
    scenario the fingerprint exists to close."""
    db = tmp_path / "cp.sqlite"
    legacy = SqliteCheckpointStore(db)  # None: schema-agnostic writer
    legacy.save_checkpoint("t1", "", "c1", None, ("json", b"{}"), ("json", b"m"))
    legacy.close()
    with pytest.raises(CheckpointSchemaError, match="pre-fingerprint"):
        SqliteCheckpointStore(db, graph_schema_fingerprint(["state"]))


@pytest.mark.unit
def test_empty_store_restamps_instead_of_rejecting(tmp_path: Path) -> None:
    """A database with NO checkpoint rows adopts the current fingerprint
    (nothing can be lost) — only databases that already hold checkpoints
    can mismatch. A schema change between two runs that never wrote a
    checkpoint must not brick the file."""
    db = tmp_path / "cp.sqlite"
    SqliteCheckpointStore(db, graph_schema_fingerprint(["state", "output"])).close()
    fp_new = graph_schema_fingerprint(["state"])
    restamped = SqliteCheckpointStore(db, fp_new)  # empty → re-stamp, no error
    restamped.save_checkpoint(
        "t1", "", "c1", None, ("json", b"{}"), ("json", b"m")
    )
    restamped.close()
    SqliteCheckpointStore(db, fp_new).close()  # sticks after a write


@pytest.mark.unit
def test_fingerprint_is_order_insensitive_and_channel_bound() -> None:
    assert graph_schema_fingerprint(["b", "a"]) == graph_schema_fingerprint(
        ["a", "b"]
    )
    assert graph_schema_fingerprint(["a"]) != graph_schema_fingerprint(["a", "b"])


# --- selection ----------------------------------------------------------------------


@pytest.mark.unit
def test_build_checkpointer_choices(tmp_path: Path) -> None:
    db = tmp_path / "cp.sqlite"
    assert build_checkpointer("none", db) is None
    memory = build_checkpointer("memory", db)
    assert isinstance(memory, InMemorySaver)
    assert not isinstance(memory, FoundrySqliteSaver)
    sqlite_saver = build_checkpointer("sqlite", db)
    assert isinstance(sqlite_saver, FoundrySqliteSaver)
    sqlite_saver.close()  # type: ignore[union-attr]
    with pytest.raises(CompileError) as excinfo:
        build_checkpointer("postgres", db)
    assert "memory" in str(excinfo.value) and "sqlite" in str(excinfo.value)


@pytest.mark.unit
def test_default_checkpoint_db_is_per_project_under_foundry_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "fh"))
    assert default_checkpoint_db("hello") == (
        tmp_path / "fh" / "checkpoints" / "hello.sqlite"
    )


# --- cross-process resume on a real StateGraph ---------------------------------------


class _State(TypedDict, total=False):
    n: int
    msgs: list[FoundryMessage]


def _msg(text: str) -> FoundryMessage:
    return FoundryMessage(role=MessageRole.USER, content=[TextBlock(text=text)])


def _build_app(saver: FoundrySqliteSaver, calls: dict[str, int], *, boom: bool) -> Any:
    graph: StateGraph[_State] = StateGraph(_State)

    async def node_a(state: _State) -> dict[str, Any]:
        calls["a"] += 1
        return {"n": state.get("n", 0) + 1, "msgs": [_msg("from-a")]}

    async def node_b(state: _State) -> dict[str, Any]:
        calls["b"] += 1
        if boom:
            raise RuntimeError("simulated kill mid-run")
        return {"n": state["n"] + 1}

    graph.add_node("a", node_a)
    graph.add_node("b", node_b)
    graph.add_edge(START, "a")
    graph.add_edge("a", "b")
    graph.add_edge("b", END)
    return graph.compile(checkpointer=saver)


@pytest.mark.unit
async def test_sqlite_saver_survives_process_death_and_resumes(
    tmp_path: Path,
) -> None:
    """Kill after node a committed -> new saver instance (fresh 'process')
    over the same file resumes at node b WITHOUT re-running a, and pydantic
    values in the checkpointed state round-trip through serde."""
    db = tmp_path / "cp.sqlite"
    config: Any = {"configurable": {"thread_id": "run-1"}}
    calls = {"a": 0, "b": 0}

    saver1 = FoundrySqliteSaver(SqliteCheckpointStore(db))
    app1 = _build_app(saver1, calls, boom=True)
    with pytest.raises(RuntimeError, match="simulated kill"):
        await app1.ainvoke({"n": 0}, config)
    saver1.close()
    assert calls == {"a": 1, "b": 1}

    saver2 = FoundrySqliteSaver(SqliteCheckpointStore(db))
    app2 = _build_app(saver2, calls, boom=False)
    snapshot = await app2.aget_state(config)
    assert snapshot.next == ("b",)  # pending node persisted across processes
    final = await app2.ainvoke(None, config)
    saver2.close()

    assert calls["a"] == 1, "completed node must NOT re-run on resume"
    assert calls["b"] == 2
    assert final["n"] == 2
    # serde round-trip: the checkpointed pydantic message came back typed
    assert isinstance(final["msgs"][0], FoundryMessage)
    assert final["msgs"][0].content[0].text == "from-a"


@pytest.mark.unit
async def test_completed_thread_has_no_pending_nodes(tmp_path: Path) -> None:
    db = tmp_path / "cp.sqlite"
    config: Any = {"configurable": {"thread_id": "run-2"}}
    calls = {"a": 0, "b": 0}
    saver = FoundrySqliteSaver(SqliteCheckpointStore(db))
    app = _build_app(saver, calls, boom=False)
    await app.ainvoke({"n": 0}, config)
    saver.close()

    fresh = FoundrySqliteSaver(SqliteCheckpointStore(db))
    app2 = _build_app(fresh, calls, boom=False)
    snapshot = await app2.aget_state(config)
    assert snapshot.next == ()  # nothing to resume — run completed
    assert snapshot.values["n"] == 2
    fresh.close()
