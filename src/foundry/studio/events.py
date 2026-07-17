"""Studio event envelope + SSE encoding (docs/72 § Observability of the
studio itself).

Two things live here:

1. :class:`StudioEvent` — the ``studio.*`` structured-event envelope.
   Emitted through :func:`foundry.observability.events.dispatch_event`,
   which mirrors it into the SQLite store's ``studio_events`` table so
   studio actions sit beside forge/human ones in the local audit surface.
2. SSE helpers for the studio's own streams (forge trajectories, chat
   sessions, task progress). Frames reuse the docs/70 encoder
   (:func:`foundry.api.streaming.encode_sse`); the stream-scoped ``id:``
   drives ``Last-Event-ID`` resume.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from foundry.api.streaming import encode_sse
from foundry.observability.events import dispatch_event

STUDIO_EVENT_KINDS = (
    "studio.config_saved",
    "studio.rollback",
    "studio.sandbox_refused",
    "studio.forge_launched",
    "studio.approval_resolved",
    "studio.provider_key_saved",
    "studio.provider_key_deleted",
)


class StudioEvent(BaseModel):
    """One studio control-plane act, mirrored to the observability store."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event: str
    project: str = ""
    studio_request_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


def emit_studio_event(
    event: str,
    *,
    project: str = "",
    studio_request_id: str = "",
    **payload: Any,
) -> StudioEvent:
    """Build + dispatch a ``studio.*`` event through the standard
    observability transports (SQLite mirror et al.)."""
    record = StudioEvent(
        event=event,
        project=project,
        studio_request_id=studio_request_id,
        payload=payload,
    )
    dispatch_event(record)
    return record


class EventLog:
    """An append-only, sequence-numbered event log with live fan-out.

    The substrate for every studio-owned SSE stream (forge runs, chat
    sessions, background tasks): ``append`` assigns the next stream-scoped
    sequence and wakes subscribers; ``subscribe`` replays history from
    ``from_sequence`` then hands over to the live queue — the docs/70
    Last-Event-ID contract, applied to studio streams.
    """

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self._subscribers: set[asyncio.Queue[dict[str, Any] | None]] = set()
        self._closed = False

    def append(self, data: dict[str, Any]) -> dict[str, Any]:
        stamped = {**data, "sequence": len(self.events)}
        self.events.append(stamped)
        for queue in list(self._subscribers):
            queue.put_nowait(stamped)
        return stamped

    def close(self) -> None:
        self._closed = True
        for queue in list(self._subscribers):
            queue.put_nowait(None)

    @property
    def closed(self) -> bool:
        return self._closed

    async def subscribe(
        self, from_sequence: int = 0
    ) -> AsyncIterator[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            next_seq = max(0, from_sequence)
            while next_seq < len(self.events):
                yield self.events[next_seq]
                next_seq += 1
            if self._closed:
                return
            while True:
                item = await queue.get()
                if item is None:
                    return
                if int(item.get("sequence", -1)) < next_seq:
                    continue
                yield item
                next_seq = int(item["sequence"]) + 1
        finally:
            self._subscribers.discard(queue)


async def sse_log_stream(
    log: EventLog,
    from_sequence: int = 0,
    *,
    terminal_events: frozenset[str] = frozenset(),
) -> AsyncIterator[str]:
    """SSE body generator over an :class:`EventLog`. Ends after a terminal
    event (when given) or when the log closes."""
    async for data in log.subscribe(from_sequence):
        yield encode_sse(data)
        if terminal_events and data.get("event") in terminal_events:
            return


def resume_sequence(last_event_id: str | None, from_sequence: int) -> int:
    """Merge the ``Last-Event-ID`` header with an explicit query param:
    replay starts AFTER the last id the client saw."""
    if last_event_id is not None:
        try:
            return int(last_event_id) + 1
        except ValueError:
            return from_sequence
    return from_sequence


__all__ = [
    "STUDIO_EVENT_KINDS",
    "EventLog",
    "StudioEvent",
    "emit_studio_event",
    "resume_sequence",
    "sse_log_stream",
]
