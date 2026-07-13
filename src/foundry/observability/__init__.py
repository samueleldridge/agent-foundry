"""Observability surface: tracing, metrics, SQLite mirror, redaction,
run artifacts (docs/80)."""

from foundry.observability.artifacts import RunArtifactWriter
from foundry.observability.events import (
    ObservabilityDispatcher,
    dispatch_event,
    event_attributes,
    get_store,
    reset_dispatcher,
)
from foundry.observability.metrics import (
    MetricsRecorder,
    get_metrics_recorder,
    reset_metrics_recorder,
)
from foundry.observability.redaction import redact_attributes, truncate_preview
from foundry.observability.store import (
    SCHEMA_VERSION,
    ObservabilityStore,
    observability_db_path,
    parse_since,
)
from foundry.observability.tracing import (
    configure_observability,
    foundry_span,
    set_span_attributes,
    worker_id,
)

__all__ = [
    "SCHEMA_VERSION",
    "MetricsRecorder",
    "ObservabilityDispatcher",
    "ObservabilityStore",
    "RunArtifactWriter",
    "configure_observability",
    "dispatch_event",
    "event_attributes",
    "foundry_span",
    "get_metrics_recorder",
    "get_store",
    "observability_db_path",
    "parse_since",
    "redact_attributes",
    "reset_dispatcher",
    "reset_metrics_recorder",
    "set_span_attributes",
    "truncate_preview",
    "worker_id",
]
