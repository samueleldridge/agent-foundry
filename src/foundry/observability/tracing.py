"""OTel span surface for the runtime (docs/01 § Observability event spec).

Phase 3 scope: the ``foundry.run`` / ``foundry.node`` / ``foundry.llm`` spans
with the docs/01 attribute spec, emitted through the OpenTelemetry API only.
With no SDK TracerProvider installed these are no-ops; exporter/metrics
wiring is Phase 9 (``foundry.observability`` will own the OTLP setup).

Attribute hygiene: None values are dropped; non-primitive values (Decimal,
enums, paths) are coerced to ``str``. Never pass secrets — attributes end up
in whatever backend the operator points OTLP at.
"""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span

_TRACER_NAME = "foundry"


def worker_id() -> str:
    """``hostname:pid`` — the docs/01 ``foundry.run`` worker dimension."""
    return f"{socket.gethostname()}:{os.getpid()}"


def _clean(attributes: dict[str, Any]) -> dict[str, str | int | float | bool]:
    cleaned: dict[str, str | int | float | bool] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        if isinstance(value, str | int | float | bool):
            cleaned[key] = value
        else:
            cleaned[key] = str(value)
    return cleaned


def set_span_attributes(span: Span, attributes: dict[str, Any]) -> None:
    """Set post-hoc attributes (e.g. token counts known only after the
    call) with the same hygiene rules as span creation."""
    for key, value in _clean(attributes).items():
        span.set_attribute(key, value)


@contextmanager
def foundry_span(name: str, attributes: dict[str, Any]) -> Iterator[Span]:
    """Start a span named per the docs/01 taxonomy (``foundry.run`` /
    ``foundry.node`` / ``foundry.llm`` / ...). Exceptions are recorded on
    the span and re-raised; the tracer is resolved lazily so a provider
    installed after import (tests, Phase 9 setup) is honoured."""
    tracer = trace.get_tracer(_TRACER_NAME)
    with tracer.start_as_current_span(name, attributes=_clean(attributes)) as span:
        yield span


_CONFIGURED_MODE: str | None = None


def configure_observability(*, mode: str | None = None) -> str:
    """Install the OTel SDK per ``FOUNDRY_TRACING`` (docs/80 § Backend
    integration patterns). Idempotent per process — the first call wins
    (OTel forbids replacing a global TracerProvider).

    Modes: ``off`` (default — API no-ops; the SQLite mirror and run
    artifacts still capture everything), ``console``, ``otel`` (OTLP),
    ``langsmith``, ``langfuse``. Metrics export is wired for ``otel`` and
    ``console``; the LangSmith/Langfuse ingests are trace-only.
    """
    global _CONFIGURED_MODE
    if _CONFIGURED_MODE is not None:
        return _CONFIGURED_MODE
    resolved = (mode or os.environ.get("FOUNDRY_TRACING", "")).strip().lower()
    if resolved in ("", "off", "none", "0", "false"):
        _CONFIGURED_MODE = "off"
        return "off"

    from opentelemetry import metrics as otel_metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import (
        ConsoleMetricExporter,
        MetricExporter,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from foundry.observability import exporters

    metric_exporter: MetricExporter | None
    if resolved == "console":
        span_exporter = exporters.build_console_exporter()
        metric_exporter = ConsoleMetricExporter()
    elif resolved == "otel":
        span_exporter = exporters.build_otlp_exporter()
        metric_exporter = exporters.build_otlp_metric_exporter()
    elif resolved == "langsmith":
        span_exporter = exporters.build_langsmith_exporter()
        metric_exporter = None
    elif resolved == "langfuse":
        span_exporter = exporters.build_langfuse_exporter()
        metric_exporter = None
    else:
        from foundry.core.errors import ConfigError

        raise ConfigError(
            f"unknown FOUNDRY_TRACING mode {resolved!r}: expected off, console, "
            "otel, langsmith, or langfuse",
            context={"mode": resolved},
        )

    resource = Resource.create(
        {"service.name": os.environ.get("OTEL_SERVICE_NAME", "foundry")}
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(provider)
    if metric_exporter is not None:
        reader = PeriodicExportingMetricReader(metric_exporter)
        otel_metrics.set_meter_provider(
            MeterProvider(resource=resource, metric_readers=[reader])
        )
    _CONFIGURED_MODE = resolved
    return resolved


__all__ = [
    "configure_observability",
    "foundry_span",
    "set_span_attributes",
    "worker_id",
]
