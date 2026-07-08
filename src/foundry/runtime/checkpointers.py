"""Checkpointer selection + SQLite persistence (docs/03 § Phase 3).

Two halves, split by the import boundary:

- THIS module is langgraph-free (stdlib ``sqlite3`` only). It owns the
  ``CheckpointerChoice`` vocabulary, the default on-disk location, and
  ``SqliteCheckpointStore`` — a dumb persistence layer for serialized
  checkpoint rows.
- ``foundry.runtime._langgraph_types`` (an allowlisted langgraph importer)
  bridges the store into a ``BaseCheckpointSaver`` the StateGraph consumes.

The SQLite layout mirrors the three mappings LangGraph's in-memory saver
keeps: checkpoints, pending writes, channel blobs. Values are stored as the
serde's ``(type, bytes)`` pairs — this module never deserializes them.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from foundry.storage.paths import foundry_home

CHECKPOINTER_CHOICES = ("memory", "sqlite", "none")
"""`foundry run --checkpoint` vocabulary. postgres lands with Tier 7 work."""


def default_checkpoint_db(project: str) -> Path:
    """Per-project checkpoint database under the foundry home:
    ``~/.foundry/checkpoints/<project>.sqlite`` (FOUNDRY_HOME-aware).
    Threads inside are keyed by run id, so one file serves every run."""
    return foundry_home() / "checkpoints" / f"{project}.sqlite"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS checkpoints (
    thread_id            TEXT NOT NULL,
    checkpoint_ns        TEXT NOT NULL,
    checkpoint_id        TEXT NOT NULL,
    parent_checkpoint_id TEXT,
    ckpt_type            TEXT NOT NULL,
    ckpt                 BLOB NOT NULL,
    meta_type            TEXT NOT NULL,
    meta                 BLOB NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id)
);
CREATE TABLE IF NOT EXISTS writes (
    thread_id     TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    checkpoint_id TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    idx           INTEGER NOT NULL,
    channel       TEXT NOT NULL,
    value_type    TEXT NOT NULL,
    value         BLOB NOT NULL,
    task_path     TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
);
CREATE TABLE IF NOT EXISTS blobs (
    thread_id     TEXT NOT NULL,
    checkpoint_ns TEXT NOT NULL,
    channel       TEXT NOT NULL,
    version       TEXT NOT NULL,
    blob_type     TEXT NOT NULL,
    blob          BLOB NOT NULL,
    PRIMARY KEY (thread_id, checkpoint_ns, channel, version)
);
"""


class SqliteCheckpointStore:
    """Serialized-checkpoint persistence. One row per checkpoint / pending
    write / channel blob; ``(type, bytes)`` values pass through opaquely."""

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- writes ---------------------------------------------------------------

    def save_checkpoint(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        parent_checkpoint_id: str | None,
        ckpt: tuple[str, bytes],
        meta: tuple[str, bytes],
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?,?,?,?)",
            (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
             ckpt[0], ckpt[1], meta[0], meta[1]),
        )
        self._conn.commit()

    def save_write(
        self,
        thread_id: str,
        checkpoint_ns: str,
        checkpoint_id: str,
        task_id: str,
        idx: int,
        channel: str,
        value: tuple[str, bytes],
        task_path: str,
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO writes VALUES (?,?,?,?,?,?,?,?,?)",
            (thread_id, checkpoint_ns, checkpoint_id, task_id, idx,
             channel, value[0], value[1], task_path),
        )
        self._conn.commit()

    def save_blob(
        self,
        thread_id: str,
        checkpoint_ns: str,
        channel: str,
        version: str,
        blob: tuple[str, bytes],
    ) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO blobs VALUES (?,?,?,?,?,?)",
            (thread_id, checkpoint_ns, channel, version, blob[0], blob[1]),
        )
        self._conn.commit()

    def delete_thread(self, thread_id: str) -> None:
        for table in ("checkpoints", "writes", "blobs"):
            self._conn.execute(
                f"DELETE FROM {table} WHERE thread_id = ?", (thread_id,)
            )
        self._conn.commit()

    # --- reads (hydration at saver construction) --------------------------------

    def load_checkpoints(
        self,
    ) -> list[tuple[str, str, str, str | None, tuple[str, bytes], tuple[str, bytes]]]:
        rows = self._conn.execute(
            "SELECT thread_id, checkpoint_ns, checkpoint_id, "
            "parent_checkpoint_id, ckpt_type, ckpt, meta_type, meta "
            "FROM checkpoints"
        ).fetchall()
        return [
            (t, ns, cid, parent, (ct, cb), (mt, mb))
            for t, ns, cid, parent, ct, cb, mt, mb in rows
        ]

    def load_writes(
        self,
    ) -> list[tuple[str, str, str, str, int, str, tuple[str, bytes], str]]:
        rows = self._conn.execute(
            "SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, idx, "
            "channel, value_type, value, task_path FROM writes"
        ).fetchall()
        return [
            (t, ns, cid, task, idx, ch, (vt, vb), path)
            for t, ns, cid, task, idx, ch, vt, vb, path in rows
        ]

    def load_blobs(self) -> list[tuple[str, str, str, str, tuple[str, bytes]]]:
        rows = self._conn.execute(
            "SELECT thread_id, checkpoint_ns, channel, version, blob_type, blob "
            "FROM blobs"
        ).fetchall()
        return [(t, ns, ch, v, (bt, bb)) for t, ns, ch, v, bt, bb in rows]

    def close(self) -> None:
        self._conn.close()


__all__ = [
    "CHECKPOINTER_CHOICES",
    "SqliteCheckpointStore",
    "default_checkpoint_db",
]
