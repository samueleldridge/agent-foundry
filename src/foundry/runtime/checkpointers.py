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

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path

from foundry.core.errors import CheckpointSchemaError
from foundry.storage.paths import foundry_home

CHECKPOINTER_CHOICES = ("memory", "sqlite", "none")
"""`foundry run --checkpoint` vocabulary. postgres lands with Tier 7 work."""

CHECKPOINT_SCHEMA_VERSION = 1
"""Bump when the persisted checkpoint LAYOUT itself changes shape. The
graph-channel set is hashed separately per compile (see
:func:`graph_schema_fingerprint`)."""


def graph_schema_fingerprint(channel_names: Iterable[str]) -> str:
    """Fingerprint of the compiled graph's channel set + the checkpoint
    schema version. Stamped into the SQLite store at write; a resume whose
    fingerprint differs fails LOUDLY instead of silently rehydrating stale
    channels (Phase 7 review finding 4 — e.g. Phase 3's ``conv`` channel
    vs Phase 7's ``conv__<agent>`` would otherwise resume with a fresh
    conversation)."""
    payload = json.dumps(
        {
            "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
            "channels": sorted(channel_names),
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


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
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

_FINGERPRINT_KEY = "schema_fingerprint"


class SqliteCheckpointStore:
    """Serialized-checkpoint persistence. One row per checkpoint / pending
    write / channel blob; ``(type, bytes)`` values pass through opaquely."""

    def __init__(
        self, path: Path, schema_fingerprint: str | None = None
    ) -> None:
        """``schema_fingerprint`` (see :func:`graph_schema_fingerprint`)
        binds the store to ONE graph channel schema: an empty store is
        stamped with it; a store already holding checkpoints written under
        a DIFFERENT (or pre-fingerprint) schema raises
        :class:`CheckpointSchemaError` instead of resuming silently.
        ``None`` skips the check (schema-agnostic access, e.g. tooling)."""
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(_SCHEMA)
        self._conn.commit()
        if schema_fingerprint is not None:
            self._enforce_schema_fingerprint(schema_fingerprint)

    def _enforce_schema_fingerprint(self, fingerprint: str) -> None:
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key = ?", (_FINGERPRINT_KEY,)
        ).fetchone()
        stored: str | None = row[0] if row else None
        has_checkpoints = bool(
            self._conn.execute(
                "SELECT EXISTS(SELECT 1 FROM checkpoints)"
            ).fetchone()[0]
        )
        if has_checkpoints and stored != fingerprint:
            self._conn.close()
            raise CheckpointSchemaError(
                f"checkpoint database {self.path} holds checkpoints written "
                "for a different graph schema (stored fingerprint: "
                f"{stored or '(pre-fingerprint checkpoint)'}; current: "
                f"{fingerprint}) — the checkpoint predates the current "
                "graph schema and cannot be resumed. Rerun WITHOUT --run-id "
                "to start a fresh run, or clear the stale checkpoints by "
                f"deleting {self.path}",
                context={
                    "path": str(self.path),
                    "stored_fingerprint": stored,
                    "current_fingerprint": fingerprint,
                    "checkpoint_schema_version": CHECKPOINT_SCHEMA_VERSION,
                },
            )
        if stored != fingerprint:
            # New database, or an EMPTY one whose schema moved on: (re)stamp
            # — nothing can be lost while no checkpoint rows exist.
            self._conn.execute(
                "INSERT OR REPLACE INTO meta VALUES (?, ?)",
                (_FINGERPRINT_KEY, fingerprint),
            )
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
        blobs: Iterable[tuple[str, str, tuple[str, bytes]]] | None = None,
    ) -> None:
        """Persist a checkpoint row and its channel blobs in ONE transaction.

        ``blobs`` is ``[(channel, version, (type, bytes)), ...]``. A crash
        between the blob writes and the checkpoint row must never leave a
        checkpoint whose channel values are missing — rehydration would
        silently resume from a partial state (Phase 3 review finding 1) —
        so the whole mirror commits atomically or not at all.
        """
        with self._conn:
            self._conn.execute(
                "INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?,?,?,?,?)",
                (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id,
                 ckpt[0], ckpt[1], meta[0], meta[1]),
            )
            for channel, version, blob in blobs or []:
                self._conn.execute(
                    "INSERT OR REPLACE INTO blobs VALUES (?,?,?,?,?,?)",
                    (thread_id, checkpoint_ns, channel, version,
                     blob[0], blob[1]),
                )

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
    "CHECKPOINT_SCHEMA_VERSION",
    "SqliteCheckpointStore",
    "default_checkpoint_db",
    "graph_schema_fingerprint",
]
