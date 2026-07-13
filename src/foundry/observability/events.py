"""RunEvent → observability dispatch (docs/80 § The three transports).

Every event the runtime's :class:`EventEmitter` produces flows through
:func:`dispatch_event`, which fans out to three handlers:

1. **Span mirror** — subsystems that emit events but hold no span of their
   own (tool, handoff, state_transition, function_node, connection, embed,
   cache, retrieval, rerank, memory, approval) get a retroactive OTel span
   per completed operation: start time is back-computed from the event's
   ``latency_ms``, parenting follows whatever span is current at emit time
   (``foundry.node`` / ``foundry.llm`` in the runtime hot path).
   ``foundry.run`` / ``foundry.node`` / ``foundry.llm`` / ``foundry.eval``
   keep their native spans (created where the work happens) and are NOT
   mirrored here.
2. **Metrics** — :mod:`foundry.observability.metrics`.
3. **SQLite mirror** — :mod:`foundry.observability.store` (disable with
   ``FOUNDRY_OBS_MIRROR=off``).

Failure policy (docs/80 § Failure modes): a handler exception must never
take a run down — it is swallowed, logged once per handler as
``observability.degraded``, and the handler stays installed.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any

import structlog
from opentelemetry import trace
from pydantic import BaseModel

from foundry.core.events import (
    ApprovalRequiredEvent,
    ApprovalResolved,
    ConnectionEvent,
    EmbedCall,
    FunctionNodeCompleted,
    Handoff,
    LLMCallStarted,
    MemoryConsolidate,
    MemoryRead,
    MemoryWriteEvent,
    RerankEvent,
    RetrievalEvent,
    RunCancelledEvent,
    RunCompleted,
    RunFailed,
    RunStarted,
    SemanticCacheHitEvent,
    SemanticCacheInvalidate,
    SemanticCacheMiss,
    SemanticCacheStore,
    StateTransition,
    ToolCacheHit,
    ToolCacheMiss,
    ToolCacheStore,
    ToolCompleted,
)
from foundry.observability.metrics import get_metrics_recorder
from foundry.observability.redaction import redact_attributes
from foundry.observability.store import ObservabilityStore, observability_db_path

_log = structlog.get_logger("foundry.observability")

_TRACER_NAME = "foundry"

# Event classes → mirrored span name (docs/80 § trace tree + docs/03 § Phase 9
# deliverable list). Instant events get zero-duration spans; events carrying
# latency_ms get their start time back-computed.
_SPAN_NAMES: dict[type[BaseModel], str] = {
    ToolCompleted: "foundry.tool",
    Handoff: "foundry.handoff",
    StateTransition: "foundry.state_transition",
    FunctionNodeCompleted: "foundry.function_node",
    ConnectionEvent: "foundry.connection",
    EmbedCall: "foundry.embed",
    SemanticCacheHitEvent: "foundry.cache.semantic",
    SemanticCacheMiss: "foundry.cache.semantic",
    SemanticCacheStore: "foundry.cache.semantic",
    SemanticCacheInvalidate: "foundry.cache.semantic",
    ToolCacheHit: "foundry.cache.tool",
    ToolCacheMiss: "foundry.cache.tool",
    ToolCacheStore: "foundry.cache.tool",
    RetrievalEvent: "foundry.retrieval",
    RerankEvent: "foundry.rerank",
    MemoryRead: "foundry.memory",
    MemoryWriteEvent: "foundry.memory",
    MemoryConsolidate: "foundry.memory",
    ApprovalRequiredEvent: "foundry.approval",
    ApprovalResolved: "foundry.approval",
}

# Event payload fields that never become span attributes (bulky or typed).
_ATTR_EXCLUDE = {
    "prompt_messages",
    "final_output",
    "delta",
    "connection_descriptor",
    "before_ids",
    "after_ids",
    "context",
    "error",
}


class _RunInfo:
    """Per-run correlation shared by the metric handler: project comes from
    run.started; provider/model from the last llm.started per agent."""

    __slots__ = ("llm", "project")

    def __init__(self) -> None:
        self.project = ""
        self.llm: dict[str, tuple[str, str]] = {}


class ObservabilityDispatcher:
    """Fan-out with per-handler degradation guards (docs/80 § Failure
    modes: exporter failure surfaces as ``observability.degraded``, never
    as a run failure)."""

    def __init__(self) -> None:
        self._handlers: list[tuple[str, Callable[[BaseModel], None]]] = []
        self._degraded: set[str] = set()

    def add_handler(self, name: str, handler: Callable[[BaseModel], None]) -> None:
        self._handlers.append((name, handler))

    def clear(self) -> None:
        self._handlers.clear()
        self._degraded.clear()

    def dispatch(self, event: BaseModel) -> None:
        for name, handler in self._handlers:
            try:
                handler(event)
            except Exception as exc:  # degradation guard by design (docs/80)
                if name not in self._degraded:
                    self._degraded.add(name)
                    _log.warning(
                        "observability.degraded",
                        subsystem=name,
                        error=f"{type(exc).__name__}: {exc}",
                    )


def event_attributes(event: BaseModel) -> dict[str, Any]:
    """Flatten an event into span-safe attributes: bulky fields excluded,
    denylisted keys + secret-shaped values dropped, previews truncated,
    non-primitives coerced by the span layer."""
    data = event.model_dump(mode="json", exclude=_ATTR_EXCLUDE, exclude_none=True)
    if isinstance(event, ConnectionEvent):
        descriptor = event.connection_descriptor
        data["connection_ref"] = descriptor.ref
        data["slot"] = descriptor.slot
        data["auth_scheme"] = descriptor.auth_scheme.value
        data["config_hash"] = descriptor.config_hash
        if descriptor.principal is not None:
            data["principal"] = descriptor.principal
    redacted = redact_attributes(data)
    out: dict[str, Any] = {}
    for key, value in redacted.items():
        if isinstance(value, dict):
            for sub_key, sub_value in value.items():
                if isinstance(sub_value, str | int | float | bool):
                    out[f"{key}.{sub_key}"] = sub_value
        elif isinstance(value, list):
            out[key] = ",".join(str(item) for item in value)
        else:
            out[key] = value
    return out


def _span_mirror(event: BaseModel) -> None:
    name = _SPAN_NAMES.get(type(event))
    if name is None:
        return
    timestamp = getattr(event, "timestamp", None)
    if timestamp is None:
        return
    end_ns = int(timestamp.timestamp() * 1_000_000_000)
    latency_ms = getattr(event, "latency_ms", 0) or 0
    start_ns = end_ns - int(latency_ms) * 1_000_000
    tracer = trace.get_tracer(_TRACER_NAME)
    attributes = {
        key: value if isinstance(value, str | int | float | bool) else str(value)
        for key, value in event_attributes(event).items()
    }
    span = tracer.start_span(name, start_time=start_ns, attributes=attributes)
    span.end(end_time=end_ns)


class _Tracker:
    """Keeps the per-run info the metrics handler needs and forwards to the
    store + recorder. Terminal events evict their run's entry."""

    def __init__(self) -> None:
        self.runs: dict[str, _RunInfo] = {}

    def info(self, event: BaseModel) -> _RunInfo:
        run_id = str(getattr(event, "run_id", ""))
        return self.runs.setdefault(run_id, _RunInfo())

    def observe(self, event: BaseModel) -> tuple[str, str, str]:
        """Returns (project, provider, model) context for this event."""
        info = self.info(event)
        if isinstance(event, RunStarted):
            info.project = event.project
        elif isinstance(event, LLMCallStarted):
            info.llm[event.agent_name] = (event.provider, event.model)
        provider, model = info.llm.get(getattr(event, "agent_name", ""), ("", ""))
        project = info.project
        if isinstance(event, RunCompleted | RunFailed | RunCancelledEvent):
            self.runs.pop(str(event.run_id), None)
        return project, provider, model


_tracker = _Tracker()
_dispatcher = ObservabilityDispatcher()
_installed = False
_store: ObservabilityStore | None = None


def _mirror_enabled() -> bool:
    return os.environ.get("FOUNDRY_OBS_MIRROR", "").strip().lower() not in (
        "off",
        "0",
        "false",
    )


def get_store() -> ObservabilityStore:
    """The process-wide store bound to the current ``FOUNDRY_HOME``. A
    changed home (tests re-pointing ``FOUNDRY_HOME``) rebinds automatically."""
    global _store
    expected = observability_db_path()
    if _store is None or _store.db_path != expected:
        if _store is not None:
            _store.close()
        _store = ObservabilityStore(expected)
    return _store


def _metrics_handler(event: BaseModel) -> None:
    project, provider, model = _tracker.observe(event)
    get_metrics_recorder().handle(event, project=project, provider=provider, model=model)


def _store_handler(event: BaseModel) -> None:
    if _mirror_enabled():
        get_store().record_event(event)


def _ensure_installed() -> None:
    global _installed
    if _installed:
        return
    _installed = True
    _dispatcher.add_handler("span_mirror", _span_mirror)
    _dispatcher.add_handler("sqlite_mirror", _store_handler)
    # metrics last: its tracker pops terminal runs, and the store keeps its
    # own correlation state, so ordering only matters for the shared tracker.
    _dispatcher.add_handler("metrics", _metrics_handler)


def dispatch_event(event: BaseModel) -> None:
    """The runtime EventEmitter's observability hook: called once per
    emitted RunEvent, after the caller's own sink."""
    _ensure_installed()
    _dispatcher.dispatch(event)


def reset_dispatcher() -> None:
    """Testing hook: drop installed handlers, correlation state, and the
    cached store so a fresh FOUNDRY_HOME starts clean."""
    global _installed, _store
    _dispatcher.clear()
    _tracker.runs.clear()
    if _store is not None:
        _store.close()
    _store = None
    _installed = False


__all__ = [
    "ObservabilityDispatcher",
    "dispatch_event",
    "event_attributes",
    "get_store",
    "reset_dispatcher",
]
