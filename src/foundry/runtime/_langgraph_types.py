"""LangGraph-facing shims: graph-state schema + checkpointer bridge.

Permitted to import ``langgraph`` / ``langchain_core`` (import-boundary
lint). Everything langgraph-shaped that is not the adapter's graph wiring
lives here so ``langgraph_adapter`` stays a thin composition layer and the
rest of the runtime stays langgraph-free.
"""

from __future__ import annotations

import operator
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, TypedDict, cast

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

GraphState = dict[str, Any]
"""Phase 7: the graph-state schema is built per compile by
:func:`make_graph_state` (per-agent conv channels + reducer-backed shared
channels); this alias is the value shape nodes see."""


def _take_last(current: Any, incoming: Any) -> Any:
    """Last-value reducer that TOLERATES concurrent writers (LangGraph's
    bare LastValue channel refuses two updates in one superstep; parallel
    branches finishing together both write ``output``)."""
    return incoming


def _merge_dicts(
    current: dict[str, Any] | None, incoming: dict[str, Any] | None
) -> dict[str, Any]:
    merged = dict(current or {})
    merged.update(incoming or {})
    return merged


def make_graph_state(
    agent_names: list[str],
    owner_names: list[str],
    state_merger: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
) -> type:
    """Build the run's graph-state schema (Phase 7).

    Channels:

    - ``state`` — the project state; nodes return DELTAS and the channel
      reducer merges them through the compiled per-field reducers
      (docs/22). Sequential application per update is what gives APPEND /
      MERGE their accumulate-under-concurrency semantics.
    - ``output`` — the last finished agent's parsed output (take-last).
    - ``outputs`` — per-agent last outputs (dict-merge).
    - ``conv__<agent>`` — one PRIVATE conversation channel per agent
      (checkpointed mid tool-loop). Structural isolation: an agent's
      slices can only be handed their own channel.
    - ``route__<owner>`` / ``decision__<owner>`` — supervisor/graph
      routing state, namespaced per owner so nested flows running in
      parallel branches never collide.
    - ``hops`` — total edge traversals (operator.add increments).
    - ``approvals`` — resolved HITL approvals by approval_id (dict-merge).
    - ``escalated`` / ``flow_status`` — supervisor max-hops bookkeeping.
    """
    fields: dict[str, Any] = {
        "state": Annotated[dict[str, Any], state_merger],
        "output": Annotated[Any, _take_last],
        "outputs": Annotated[dict[str, Any], _merge_dicts],
        "hops": Annotated[int, operator.add],
        "approvals": Annotated[dict[str, Any], _merge_dicts],
        "escalated": Annotated[bool, operator.or_],
        "flow_status": Annotated[Any, _take_last],
    }
    for agent in agent_names:
        fields[f"conv__{agent}"] = Annotated[Any, _take_last]
    for owner in owner_names:
        fields[f"route__{owner}"] = Annotated[Any, _take_last]
        fields[f"decision__{owner}"] = Annotated[Any, _take_last]
    schema = TypedDict(  # type: ignore[misc]
        "FoundryGraphState", fields, total=False
    )
    return cast(type, schema)


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
    choice: str, db_path: Path, schema_fingerprint: str | None = None
) -> BaseCheckpointSaver[str] | None:
    """``--checkpoint`` value → saver instance (None disables checkpointing).

    ``schema_fingerprint`` binds the SQLite store to the current graph's
    channel schema — resuming checkpoints written under a different schema
    raises ``CheckpointSchemaError`` loudly (Phase 7 review finding 4).
    The in-memory saver never outlives the process, so it is unaffected."""
    if choice == "none":
        return None
    if choice == "memory":
        return InMemorySaver()
    if choice == "sqlite":
        return FoundrySqliteSaver(
            SqliteCheckpointStore(db_path, schema_fingerprint)
        )
    raise CompileError(
        f"unknown checkpointer {choice!r}; valid: "
        f"{', '.join(CHECKPOINTER_CHOICES)}",
        context={"received": choice, "valid": list(CHECKPOINTER_CHOICES)},
    )


__all__ = [
    "FoundrySqliteSaver",
    "GraphState",
    "build_checkpointer",
    "make_graph_state",
]
