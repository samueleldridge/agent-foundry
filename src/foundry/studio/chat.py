"""Chat machinery: per-project in-process RunManager pool + sessions
(docs/72 § Chat).

- **One RunManager per project** — the SAME ``foundry.api.runs.RunManager``
  that ``foundry serve`` uses, bound into the studio app's lifespan task
  group with a SQLite checkpointer (HITL pauses survive a studio restart).
- **Each chat message = one run.** The session SSE stream multiplexes its
  runs' RunEvents with a session-scoped sequence (``Last-Event-ID``
  resume); the original run-scoped sequence rides along as
  ``run_sequence``.
- **Conversation carry**: when the project's input model declares the
  ``turns`` convention, prior turns thread into each new run's input;
  otherwise every message is independent (the UI labels the chat
  single-turn).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, get_origin

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ValidationError

from foundry.api.runs import RunManager
from foundry.api.schemas import derive_input_model
from foundry.api.streaming import subscribe_events
from foundry.api.worker import WorkerState
from foundry.core.errors import (
    ConfigLoadError,
    ConfigValidationError,
    OrchestrationError,
    ProjectUnavailableError,
)
from foundry.core.types import RunId
from foundry.observability.logging import run_logger
from foundry.storage.paths import foundry_home
from foundry.studio.context import StudioContext
from foundry.studio.events import (
    EventLog,
    emit_studio_event,
    resume_sequence,
    sse_log_stream,
)
from foundry.studio.schemas import (
    ChatInputField,
    ChatMessageRequest,
    ChatMessageResponse,
    ChatSessionInfo,
    ResumeRequest,
    ResumeResponse,
)


@dataclass
class ChatSession:
    session_id: str
    project: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    run_ids: list[str] = field(default_factory=list)
    multi_turn: bool = False
    turns: list[dict[str, str]] = field(default_factory=list)
    log: EventLog = field(default_factory=EventLog)
    replayed: bool = False
    input_fields: list[ChatInputField] = field(default_factory=list)

    def info(self) -> ChatSessionInfo:
        return ChatSessionInfo(
            session_id=self.session_id,
            project=self.project,
            created_at=self.created_at,
            run_ids=list(self.run_ids),
            multi_turn=self.multi_turn,
            events_url=(
                f"/api/chat/{self.project}/sessions/{self.session_id}/events"
            ),
            input_fields=list(self.input_fields),
        )


@dataclass
class ProjectChat:
    project: str
    manager: RunManager
    input_model: type[BaseModel]
    sessions: dict[str, ChatSession] = field(default_factory=dict)
    input_fields: list[ChatInputField] = field(default_factory=list)


class ChatRegistry:
    """project → ProjectChat, lazily booted; sessions indexed on disk so
    reattach survives a studio restart (event logs rebuild from run
    artifacts; pending HITL pauses resume via the checkpointer)."""

    def __init__(self, ctx: StudioContext) -> None:
        self._ctx = ctx
        self._chats: dict[str, ProjectChat] = {}
        self._index_path = foundry_home() / "studio" / "chat_sessions.json"
        self._index: dict[str, dict[str, Any]] = self._load_index()

    # --- persistence ---------------------------------------------------------------

    def _load_index(self) -> dict[str, dict[str, Any]]:
        if not self._index_path.is_file():
            return {}
        try:
            loaded = json.loads(self._index_path.read_text())
        except json.JSONDecodeError:
            return {}
        return loaded if isinstance(loaded, dict) else {}

    def _save_index(self) -> None:
        self._index_path.parent.mkdir(parents=True, exist_ok=True)
        self._index_path.write_text(json.dumps(self._index, default=str))

    def _record(self, session: ChatSession) -> None:
        self._index[session.session_id] = {
            "project": session.project,
            "created_at": session.created_at.isoformat(),
            "run_ids": session.run_ids,
            "multi_turn": session.multi_turn,
        }
        self._save_index()

    # --- pool ----------------------------------------------------------------------

    def project_chat(self, project: str) -> ProjectChat:
        chat = self._chats.get(project)
        if chat is not None:
            return chat
        compiled = self._ctx.compiled(project)
        manager = RunManager(
            compiled,
            worker_state=WorkerState(),
            checkpoint=self._ctx.checkpoint,
        )
        manager.bind(self._ctx.task_group)
        input_model = derive_input_model(compiled)
        chat = ProjectChat(
            project=project,
            manager=manager,
            input_model=input_model,
            input_fields=_input_fields(input_model),
        )
        self._chats[project] = chat
        self._restore_sessions(chat)
        return chat

    def manager_for(self, project: str) -> RunManager:
        return self.project_chat(project).manager

    def invalidate(self, project: str) -> None:
        """A config save / rollback touched the project: recompile on next
        use. Sessions are re-adopted by the fresh manager (their runs are
        checkpointed / persisted)."""
        self._chats.pop(project, None)

    def pool_size(self) -> int:
        return len(self._chats)

    def managers(self) -> list[RunManager]:
        return [chat.manager for chat in self._chats.values()]

    def active_sessions(self) -> int:
        return sum(len(chat.sessions) for chat in self._chats.values())

    def _restore_sessions(self, chat: ProjectChat) -> None:
        for session_id, record in self._index.items():
            if record.get("project") != chat.project:
                continue
            if session_id in chat.sessions:
                continue
            created = record.get("created_at")
            chat.sessions[session_id] = ChatSession(
                session_id=session_id,
                project=chat.project,
                created_at=(
                    datetime.fromisoformat(created)
                    if isinstance(created, str)
                    else datetime.now(UTC)
                ),
                run_ids=list(record.get("run_ids", [])),
                multi_turn=bool(record.get("multi_turn", False)),
                input_fields=list(chat.input_fields),
            )

    # --- session ops ------------------------------------------------------------------

    def open_session(self, project: str) -> ChatSession:
        chat = self.project_chat(project)
        session = ChatSession(
            session_id=f"s_{RunId.new()}",
            project=project,
            multi_turn="turns" in chat.input_model.model_fields,
            input_fields=list(chat.input_fields),
        )
        chat.sessions[session.session_id] = session
        self._record(session)
        return session

    def session_infos(self, project: str) -> list[ChatSessionInfo]:
        """Sessions for the chat screen. An UNAVAILABLE project (missing
        runtime secrets → :class:`ProjectUnavailableError`) still lists
        its stored sessions from the on-disk index — browsing history
        never requires a compilable project (docs/72 § Failure modes).
        Other compile failures propagate unchanged."""
        try:
            chat = self.project_chat(project)
        except ProjectUnavailableError:
            return [
                self._stored_info(session_id, record)
                for session_id, record in self._index.items()
                if record.get("project") == project
            ]
        return [session.info() for session in chat.sessions.values()]

    def _stored_info(
        self, session_id: str, record: dict[str, Any]
    ) -> ChatSessionInfo:
        project = str(record.get("project", ""))
        created = record.get("created_at")
        return ChatSessionInfo(
            session_id=session_id,
            project=project,
            created_at=(
                datetime.fromisoformat(created)
                if isinstance(created, str)
                else None
            ),
            run_ids=[str(r) for r in record.get("run_ids", [])],
            multi_turn=bool(record.get("multi_turn", False)),
            events_url=f"/api/chat/{project}/sessions/{session_id}/events",
        )

    def session(self, project: str, session_id: str) -> ChatSession:
        chat = self.project_chat(project)
        session = chat.sessions.get(session_id)
        if session is None:
            raise ConfigLoadError(
                f"chat session {session_id!r} not found for project "
                f"{project!r}",
                context={"session_id": session_id, "not_found": True},
            )
        self._replay_if_cold(chat, session)
        return session

    def _replay_if_cold(self, chat: ProjectChat, session: ChatSession) -> None:
        """After a studio restart the in-memory event log is empty while
        the session has prior runs: rebuild the log from the persisted run
        artifacts so reattach replays the whole thread."""
        if session.replayed or session.log.events or not session.run_ids:
            session.replayed = True
            return
        for run_id in session.run_ids:
            for data in chat.manager.read_artifact_events(run_id):
                entry = dict(data)
                entry["run_sequence"] = entry.get("sequence")
                session.log.append(entry)
        session.replayed = True

    def start_message(
        self, project: str, session_id: str, text: str, request_id: str
    ) -> tuple[ChatSession, str]:
        chat = self.project_chat(project)
        session = self.session(project, session_id)
        input_data = _input_from_text(text, chat.input_model)
        if session.multi_turn and "turns" not in input_data:
            input_data["turns"] = list(session.turns)
        try:
            validated = chat.input_model.model_validate(input_data)
        except ValidationError as exc:
            errors = exc.errors()
            message = str(errors[0].get("msg", "invalid")) if errors else "invalid"
            field_path = (
                ".".join(str(p) for p in errors[0].get("loc", ()))
                if errors
                else ""
            )
            raise ConfigValidationError(
                "chat input failed validation against the project input "
                f"model: {message}",
                context={"field": field_path, "project": project},
                cause=exc,
            ) from exc
        live = chat.manager.start_run(validated.model_dump(mode="json"))
        run_id = str(live.run_id)
        session.run_ids.append(run_id)
        session.turns.append({"role": "user", "content": text})
        self._record(session)
        run_logger(run_id).info(
            "studio.chat_message",
            project=project,
            session_id=session_id,
            studio_request_id=request_id,
        )
        self._ctx.spawn(self._pump, chat, session, run_id)
        return session, run_id

    async def _pump(
        self,
        chat: ProjectChat,
        session: ChatSession,
        run_id: str,
        from_sequence: int = 0,
    ) -> None:
        """Multiplex one run's events into the session log. A
        ``run.completed(status=approval_pending)`` is a PAUSE, not a
        terminal event — the pump keeps following the run through the
        approval resume (docs/32)."""
        async for data in subscribe_events(
            chat.manager, run_id, from_sequence
        ):
            entry = dict(data)
            entry["run_sequence"] = entry.get("sequence")
            session.log.append(entry)
            name = str(data.get("event", ""))
            if name == "run.completed":
                if data.get("status") == "approval_pending":
                    continue
                output = data.get("final_output")
                session.turns.append(
                    {
                        "role": "assistant",
                        "content": (
                            output
                            if isinstance(output, str)
                            else json.dumps(output, default=str)
                        ),
                    }
                )
                self._record(session)
                return
            if name in ("run.failed", "run.cancelled"):
                return

    def deliver_approval(
        self,
        project: str,
        session_id: str,
        body: ResumeRequest,
        request_id: str,
    ) -> tuple[str, str]:
        chat = self.project_chat(project)
        session = self.session(project, session_id)
        target: str | None = None
        for run_id in reversed(session.run_ids):
            live = chat.manager.get(run_id)
            status = (
                live.status
                if live is not None
                else (chat.manager.read_artifact_metadata(run_id) or {}).get(
                    "status"
                )
            )
            if status == "approval_pending":
                target = run_id
                break
        if target is None:
            raise OrchestrationError(
                f"session {session_id} has no run awaiting approval",
                context={"session_id": session_id, "project": project},
            )
        was_live = chat.manager.get(target) is not None
        live_run = chat.manager.deliver_approval(
            target,
            {
                "approval_id": body.approval_id,
                "decision": body.decision,
                "reason": body.reason,
            },
        )
        if not was_live:
            # Restarted-process resume: a fresh LiveRun needs a fresh pump
            # picking up where the persisted sequence left off (the prior
            # history was already replayed into the session log).
            self._ctx.spawn(
                self._pump,
                chat,
                session,
                str(live_run.run_id),
                live_run.base_sequence,
            )
        emit_studio_event(
            "studio.approval_resolved",
            project=project,
            studio_request_id=request_id,
            run_id=target,
            approval_id=body.approval_id,
            decision=body.decision,
        )
        return target, live_run.status


def _placeholder(annotation: Any) -> Any:
    """A fill-me-in JSON value for one input field (template building)."""
    origin = get_origin(annotation) or annotation
    if origin is bool:
        return False
    if origin is int:
        return 0
    if origin is float:
        return 0.0
    if origin in (list, tuple, set, frozenset):
        return []
    if origin is dict:
        return {}
    return "..."


def _input_fields(input_model: type[BaseModel]) -> list[ChatInputField]:
    """Project input model → composer field metadata. The auto-threaded
    `turns` field is excluded (the session supplies it)."""
    schema = input_model.model_json_schema()
    required = set(schema.get("required", []))
    properties = schema.get("properties", {})
    return [
        ChatInputField(
            name=name,
            type=str(prop.get("type", "json")),
            required=name in required,
        )
        for name, prop in properties.items()
        if name != "turns"
    ]


def _input_from_text(
    text: str, input_model: type[BaseModel]
) -> dict[str, Any]:
    """Chat text → run input: a JSON object IS the input; plain text fills
    a single-required-field input (the docs/70 inject_input convention)."""
    stripped = text.strip()
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict):
        return dict(parsed)
    required = [
        name
        for name, field_info in input_model.model_fields.items()
        if field_info.is_required()
    ]
    if len(required) == 1:
        return {required[0]: text}
    template = {
        name: _placeholder(input_model.model_fields[name].annotation)
        for name in required
    }
    raise ConfigValidationError(
        "chat text must be a JSON object matching the project input schema "
        f"(required fields: {', '.join(required) or '(none)'}) — "
        f"ready-to-fill template: {json.dumps(template)}",
        context={"required_fields": required, "template": template},
    )


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.post(
        "/chat/{project}/sessions",
        response_model=ChatSessionInfo,
        status_code=201,
    )
    async def open_session(project: str) -> ChatSessionInfo:
        assert ctx.chat is not None
        ctx.project_dir(project)  # 404 before compiling
        return ctx.chat.open_session(project).info()

    @router.get(
        "/chat/{project}/sessions", response_model=list[ChatSessionInfo]
    )
    async def list_sessions(project: str) -> list[ChatSessionInfo]:
        assert ctx.chat is not None
        ctx.project_dir(project)  # 404 for unknown projects
        return ctx.chat.session_infos(project)

    @router.post(
        "/chat/{project}/sessions/{session_id}/messages",
        response_model=ChatMessageResponse,
    )
    async def post_message(
        project: str,
        session_id: str,
        body: ChatMessageRequest,
        request: Request,
    ) -> ChatMessageResponse:
        assert ctx.chat is not None
        request_id = getattr(request.state, "studio_request_id", "")
        session, run_id = ctx.chat.start_message(
            project, session_id, body.text, request_id
        )
        return ChatMessageResponse(
            session_id=session.session_id,
            run_id=run_id,
            events_url=(
                f"/api/chat/{project}/sessions/{session.session_id}/events"
            ),
        )

    @router.get("/chat/{project}/sessions/{session_id}/events")
    async def session_events(
        project: str,
        session_id: str,
        from_sequence: int = Query(0),
        last_event_id: str | None = Header(None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        assert ctx.chat is not None
        session = ctx.chat.session(project, session_id)
        start = resume_sequence(last_event_id, from_sequence)
        return StreamingResponse(
            sse_log_stream(session.log, start),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @router.post(
        "/chat/{project}/sessions/{session_id}/approvals",
        response_model=ResumeResponse,
    )
    async def resolve_approval(
        project: str,
        session_id: str,
        body: ResumeRequest,
        request: Request,
    ) -> ResumeResponse:
        assert ctx.chat is not None
        request_id = getattr(request.state, "studio_request_id", "")
        run_id, status = ctx.chat.deliver_approval(
            project, session_id, body, request_id
        )
        return ResumeResponse(
            run_id=run_id,
            status=status,
            events_url=f"/api/runs/{run_id}/events",
        )

    return router


__all__ = ["ChatRegistry", "ChatSession", "ProjectChat", "build_router"]
