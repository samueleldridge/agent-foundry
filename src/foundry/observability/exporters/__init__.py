"""Span/metric exporter selection (docs/80 § Backend integration patterns).

``FOUNDRY_TRACING`` picks the backend:

- unset / ``off`` — no SDK installed; span + metric calls are API no-ops.
- ``console``     — dev-only stdout exporter.
- ``otel``        — OTLP to ``OTEL_EXPORTER_OTLP_ENDPOINT`` (any collector).
- ``langsmith``   — OTLP to LangSmith's OTel ingest (no extra SDK needed).
- ``langfuse``    — OTLP to Langfuse's OTel ingest (no extra SDK needed).
"""

from foundry.observability.exporters.console import build_console_exporter
from foundry.observability.exporters.langfuse import build_langfuse_exporter
from foundry.observability.exporters.langsmith import build_langsmith_exporter
from foundry.observability.exporters.otlp import build_otlp_exporter, build_otlp_metric_exporter

__all__ = [
    "build_console_exporter",
    "build_langfuse_exporter",
    "build_langsmith_exporter",
    "build_otlp_exporter",
    "build_otlp_metric_exporter",
]
