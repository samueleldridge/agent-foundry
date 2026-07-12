"""SSE encoder + event subscription + WebSocket handler (docs/70).

**SSE** (``POST /stream``, ``GET /runs/{id}/events``): each RunEvent is one
frame — ``id:`` is the run-scoped sequence number, ``event:`` the tagged
event name, ``data:`` the serialised event. ``Last-Event-ID`` reconnect
replays from the persisted artifact (``events.jsonl``) and, when the run
is still live on this worker, hands over to the in-process broadcast
seamlessly (dedupe by sequence; docs/85 § SSE is worker-agnostic).

**WebSocket** (``WS /ws``): bidirectional JSON frames.
Outbound: ``{"direction": "outbound", "event": <RunEvent>}`` (plus
``welcome`` / ``error`` service frames). Inbound:
``{"direction": "inbound", "message": <InboundMessage>}`` — ``init_run``
starts a run with an explicit input dict; ``inject_input`` starts the
socket's next run from a message (chat-style: JSON text becomes the input
object; plain text fills a single-required-field input); mid-run
injection into a RUNNING graph is deferred (documented, v1.1+).
``approval_response`` resolves an HITL pause; ``cancel`` / ``pause`` stop
the run (pause = checkpointed cancel, resumable); ``resume`` re-drives a
checkpointed run.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import anyio
from fastapi import WebSocket, WebSocketDisconnect
from pydantic import BaseModel, TypeAdapter, ValidationError

from foundry.api.runs import TERMINAL_EVENTS, RunManager
from foundry.core import InboundMessage, RunId
from foundry.core.errors import FoundryError

_INBOUND: TypeAdapter[Any] = TypeAdapter(InboundMessage)


# --- SSE -------------------------------------------------------------------------


def encode_sse(data: dict[str, Any]) -> str:
    """One RunEvent → one SSE frame (docs/70 § POST /stream)."""
    return (
        f"id: {data.get('sequence', '')}\n"
        f"event: {data.get('event', 'message')}\n"
        f"data: {json.dumps(data, separators=(',', ':'))}\n\n"
    )


async def subscribe_events(
    manager: RunManager, run_id: str, from_sequence: int = 0
) -> AsyncIterator[dict[str, Any]]:
    """Persisted replay + live handover, deduped by sequence.

    Yields every event with ``sequence >= from_sequence`` in order:
    artifact first (survives process death), then this process's live
    broadcast until the run closes its stream. Callers decide when to
    stop (e.g. at a terminal event)."""
    next_seq = from_sequence
    live = manager.get(run_id)
    if live is None or live.base_sequence > next_seq:
        for data in manager.read_artifact_events(run_id, next_seq - 1):
            if int(data.get("sequence", -1)) >= next_seq:
                yield data
                next_seq = int(data["sequence"]) + 1
        live = manager.get(run_id)
    if live is None:
        return
    queue: asyncio.Queue[Any] = asyncio.Queue()
    live.subscribers.add(queue)
    try:
        # Catch up from the in-memory mirror; the queue was registered
        # FIRST, so anything emitted meanwhile is deduped below.
        while (existing := live.event_at(next_seq)) is not None:
            yield existing
            next_seq += 1
        if not live.active:
            return
        while True:
            item = await queue.get()
            if item is None:  # broadcast close sentinel
                return
            if int(item.get("sequence", -1)) < next_seq:
                continue
            yield item
            next_seq = int(item["sequence"]) + 1
    finally:
        live.subscribers.discard(queue)


async def sse_run_stream(
    manager: RunManager,
    run_id: str,
    from_sequence: int = 0,
    *,
    cancel_on_disconnect: bool = False,
) -> AsyncIterator[str]:
    """The body generator for SSE responses. Ends cleanly after the run's
    terminal event. A client disconnect before the terminal event cancels
    the run when ``cancel_on_disconnect`` (POST /stream semantics: the
    docs/70 '499' path — checkpoint persists; resume works later)."""
    terminal_seen = False
    try:
        async for data in subscribe_events(manager, run_id, from_sequence):
            yield encode_sse(data)
            if data.get("event") in TERMINAL_EVENTS:
                terminal_seen = True
                return
    finally:
        if cancel_on_disconnect and not terminal_seen:
            manager.cancel(run_id, "user_abort")


# --- WebSocket -----------------------------------------------------------------


def _outbound(event: dict[str, Any]) -> dict[str, Any]:
    return {"direction": "outbound", "event": event}


def _error_frame(message: str, **context: Any) -> dict[str, Any]:
    return {
        "direction": "outbound",
        "error": {"message": message, "context": context},
    }


def _parse_inbound(raw: dict[str, Any]) -> Any:
    """Accept the documented envelope ({'direction','message'}) and, for
    convenience, a bare InboundMessage object."""
    payload = raw.get("message") if "message" in raw else raw
    return _INBOUND.validate_python(payload)


def _input_from_message(
    message: Any, input_model: type[BaseModel]
) -> dict[str, Any]:
    """InjectInput.message → run input. JSON-object text is the input
    dict; plain text fills a single-required-field input."""
    texts = [
        getattr(block, "text", "")
        for block in message.content
        if getattr(block, "type", "") == "text"
    ]
    text = "\n".join(t for t in texts if t).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return parsed
    required = [
        name
        for name, field in input_model.model_fields.items()
        if field.is_required()
    ]
    if len(required) == 1:
        return {required[0]: text}
    raise FoundryError(
        "inject_input text must be a JSON object matching the project "
        f"input schema (required fields: {', '.join(required) or '(none)'})",
        context={"required_fields": required},
    )


class _SocketState:
    def __init__(self) -> None:
        self.current_run_id: str | None = None
        self.owned_run_ids: list[str] = []


async def handle_websocket(
    ws: WebSocket,
    manager: RunManager,
    input_model: type[BaseModel],
) -> None:
    await ws.accept()
    state = _SocketState()
    send_lock = asyncio.Lock()

    async def send(frame: dict[str, Any]) -> None:
        async with send_lock:
            await ws.send_json(frame)

    async def pump(run_id: str, from_sequence: int) -> None:
        try:
            async for data in subscribe_events(manager, run_id, from_sequence):
                await send(_outbound(data))
        except (WebSocketDisconnect, RuntimeError):
            return  # socket gone; the inbound loop handles cleanup

    async with anyio.create_task_group() as tg:

        def attach(run_id: str, from_sequence: int = 0) -> None:
            state.current_run_id = run_id
            if run_id not in state.owned_run_ids:
                state.owned_run_ids.append(run_id)
            tg.start_soon(pump, run_id, from_sequence)

        def socket_run_active() -> bool:
            if state.current_run_id is None:
                return False
            live = manager.get(state.current_run_id)
            return live is not None and live.active

        # Attach to an existing run when the URL carries one (docs/70:
        # wss://.../ws?run_id=...).
        query_run = ws.query_params.get("run_id")
        if query_run:
            from_seq = int(ws.query_params.get("from_sequence", "0"))
            attach(query_run, from_seq)
        await send(
            {
                "direction": "outbound",
                "welcome": {
                    "worker_id": manager.worker_state.worker_id,
                    "project": manager.project,
                    "next_run_id": str(RunId.new()),
                    "attached_run_id": query_run,
                },
            }
        )

        try:
            while True:
                raw = await ws.receive_json()
                try:
                    message = _parse_inbound(raw)
                except ValidationError as exc:
                    await send(
                        _error_frame(
                            "invalid inbound message",
                            detail=str(exc.errors()[0].get("msg", "")),
                        )
                    )
                    continue
                try:
                    await _dispatch_inbound(
                        message, manager, input_model, state, attach,
                        socket_run_active, send,
                    )
                except FoundryError as exc:
                    await send(
                        _error_frame(str(exc), **{
                            k: v
                            for k, v in exc.context.items()
                            if isinstance(v, str | int | float | bool)
                        })
                    )
        except WebSocketDisconnect:
            pass
        finally:
            # Kill-client policy (docs/70 § failure modes): in-flight runs
            # this socket started are cancelled; approval-pending runs stay
            # parked on the checkpointer for a later resume.
            for run_id in state.owned_run_ids:
                live = manager.get(run_id)
                if live is not None and live.active and (
                    live.status == "in_progress"
                ):
                    manager.cancel(run_id, "user_abort")
            tg.cancel_scope.cancel()


async def _dispatch_inbound(
    message: Any,
    manager: RunManager,
    input_model: type[BaseModel],
    state: _SocketState,
    attach: Any,
    socket_run_active: Any,
    send: Any,
) -> None:
    kind = message.kind
    if kind == "init_run":
        if socket_run_active():
            raise FoundryError(
                "a run is already active on this socket — cancel it or "
                "wait for its terminal event before init_run",
                context={"run_id": state.current_run_id},
            )
        validated = input_model.model_validate(message.input)
        live = manager.start_run(validated.model_dump(mode="json"))
        attach(str(live.run_id))
    elif kind == "inject_input":
        if socket_run_active():
            live_run = manager.get(state.current_run_id or "")
            if live_run is not None and live_run.status == "approval_pending":
                raise FoundryError(
                    "this run awaits an approval_response, not input",
                    context={"run_id": state.current_run_id},
                )
            raise FoundryError(
                "inject_input into a RUNNING graph is not supported in v1 "
                "(v1.1+ backlog) — it starts the socket's NEXT run once "
                "the current one reaches a terminal event",
                context={"run_id": state.current_run_id},
            )
        input_data = _input_from_message(message.message, input_model)
        validated = input_model.model_validate(input_data)
        live = manager.start_run(validated.model_dump(mode="json"))
        attach(str(live.run_id))
    elif kind == "approval_response":
        target = str(message.run_id) or state.current_run_id or ""
        live = manager.deliver_approval(
            target,
            {
                "approval_id": message.approval_id,
                "decision": message.decision,
                "reason": message.reason,
            },
        )
        if str(live.run_id) != state.current_run_id:
            attach(str(live.run_id))  # restarted-process resume path
    elif kind == "cancel":
        target = str(message.run_id) or state.current_run_id or ""
        if not manager.cancel(target, message.reason or "user_abort"):
            raise FoundryError(
                f"run {target} is not active on this worker",
                context={"run_id": target},
            )
    elif kind == "pause":
        target = str(message.run_id) or state.current_run_id or ""
        if not manager.cancel(target, "pause"):
            raise FoundryError(
                f"run {target} is not active on this worker",
                context={"run_id": target},
            )
    elif kind == "resume":
        target = str(message.run_id) or state.current_run_id or ""
        live_existing = manager.get(target)
        if live_existing is not None and live_existing.active:
            raise FoundryError(
                f"run {target} is still active; nothing to resume",
                context={"run_id": target},
            )
        live = manager.resume_run(target)
        attach(str(live.run_id), from_sequence=live.base_sequence)
    else:  # pragma: no cover — the union is exhaustive
        raise FoundryError(f"unhandled inbound kind {kind!r}", context={})


__all__ = [
    "encode_sse",
    "handle_websocket",
    "sse_run_stream",
    "subscribe_events",
]
