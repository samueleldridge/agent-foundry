"""LangGraph-facing shims: graph-state schema + checkpointer bridge.

Permitted to import ``langgraph`` / ``langchain_core`` (import-boundary
lint). Everything langgraph-shaped that is not the adapter's graph wiring
lives here so ``langgraph_adapter`` stays a thin composition layer and the
rest of the runtime stays langgraph-free.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
)
from langgraph.checkpoint.memory import InMemorySaver

from foundry.core.errors import CompileError
from foundry.runtime.checkpointers import (
    CHECKPOINTER_CHOICES,
    SqliteCheckpointStore,
)


class GraphState(TypedDict, total=False):
    """State schema for the Phase 3 StateGraph.

    ``state`` is the project's declared state dict (nodes merge their deltas
    via the compiled reducers before returning). ``output`` carries the flow
    agent's final parsed output. ``conv`` is the in-flight agent-step
    conversation bundle (messages, turn/round counters, pending response) —
    checkpointed at every node boundary so a killed run resumes mid tool
    loop / mid memory turn instead of restarting the whole agent step.
    """

    state: dict[str, Any]
    output: Any
    conv: dict[str, Any] | None


class FoundrySqliteSaver(InMemorySaver):
    """SQLite-backed checkpointer for dev runs (docs/03 § Phase 3).

    Keeps LangGraph's in-memory saver semantics verbatim and mirrors every
    committed mapping row into :class:`SqliteCheckpointStore`, rehydrating
    on construction — so a new process resumes exactly where a killed one
    stopped. Serialization stays with the saver's serde; the store only
    persists opaque ``(type, bytes)`` pairs.
    """

    def __init__(self, store: SqliteCheckpointStore) -> None:
        super().__init__()
        self._store = store
        for thread_id, ns, cid, parent, ckpt, meta in store.load_checkpoints():
            self.storage[thread_id][ns][cid] = (ckpt, meta, parent)
        for thread_id, ns, cid, task, idx, channel, value, path in (
            store.load_writes()
        ):
            self.writes[(thread_id, ns, cid)][(task, idx)] = (
                task, channel, value, path,
            )
        for thread_id, ns, channel, version, blob in store.load_blobs():
            self.blobs[(thread_id, ns, channel, version)] = blob

    def put(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        result = super().put(config, checkpoint, metadata, new_versions)
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"]["checkpoint_ns"]
        ckpt, meta, parent = self.storage[thread_id][checkpoint_ns][checkpoint["id"]]
        # Checkpoint row + channel blobs mirror in ONE SQLite transaction: a
        # crash mid-mirror must never rehydrate a checkpoint whose channel
        # values are missing (Phase 3 review finding 1).
        blob_rows = [
            (
                channel,
                str(version),
                self.blobs[(thread_id, checkpoint_ns, channel, version)],
            )
            for channel, version in new_versions.items()
        ]
        self._store.save_checkpoint(
            thread_id, checkpoint_ns, checkpoint["id"], parent, ckpt, meta,
            blobs=blob_rows,
        )
        return result

    def put_writes(
        self,
        config: RunnableConfig,
        writes: Any,
        task_id: str,
        task_path: str = "",
    ) -> None:
        super().put_writes(config, writes, task_id, task_path)
        thread_id = config["configurable"]["thread_id"]
        checkpoint_ns = config["configurable"].get("checkpoint_ns", "")
        checkpoint_id = config["configurable"]["checkpoint_id"]
        outer_key = (thread_id, checkpoint_ns, checkpoint_id)
        for (task, idx), (_task, channel, value, path) in (
            self.writes[outer_key].items()
        ):
            self._store.save_write(
                thread_id, checkpoint_ns, checkpoint_id,
                task, idx, channel, value, path,
            )

    def delete_thread(self, thread_id: str) -> None:
        super().delete_thread(thread_id)
        self._store.delete_thread(thread_id)

    def close(self) -> None:
        self._store.close()


def build_checkpointer(
    choice: str, db_path: Path
) -> BaseCheckpointSaver[str] | None:
    """``--checkpoint`` value → saver instance (None disables checkpointing)."""
    if choice == "none":
        return None
    if choice == "memory":
        return InMemorySaver()
    if choice == "sqlite":
        return FoundrySqliteSaver(SqliteCheckpointStore(db_path))
    raise CompileError(
        f"unknown checkpointer {choice!r}; valid: "
        f"{', '.join(CHECKPOINTER_CHOICES)}",
        context={"received": choice, "valid": list(CHECKPOINTER_CHOICES)},
    )


__all__ = [
    "FoundrySqliteSaver",
    "GraphState",
    "build_checkpointer",
]
