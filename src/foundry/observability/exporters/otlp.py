"""OTLP exporters (docs/80 § Backend integration patterns).

Standard OTel env vars drive the target: ``OTEL_EXPORTER_OTLP_ENDPOINT``
(default ``http://localhost:4317``), ``OTEL_EXPORTER_OTLP_PROTOCOL``
(``grpc`` default per the OTel spec; ``http/protobuf`` supported), and
``OTEL_EXPORTER_OTLP_HEADERS``. Works against any OTLP-compatible backend
(Datadog agent, Jaeger, Tempo, Honeycomb, an otel-collector, ...).
"""

from __future__ import annotations

import os

from opentelemetry.sdk.metrics.export import MetricExporter
from opentelemetry.sdk.trace.export import SpanExporter


def _protocol() -> str:
    return os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "grpc").strip().lower()


def build_otlp_exporter(
    *, endpoint: str | None = None, headers: dict[str, str] | None = None
) -> SpanExporter:
    if _protocol().startswith("http"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter as HttpSpanExporter,
        )

        return HttpSpanExporter(endpoint=endpoint, headers=headers)
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter as GrpcSpanExporter,
    )

    return GrpcSpanExporter(endpoint=endpoint, headers=headers)


def build_otlp_metric_exporter(
    *, endpoint: str | None = None, headers: dict[str, str] | None = None
) -> MetricExporter:
    if _protocol().startswith("http"):
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import (
            OTLPMetricExporter as HttpMetricExporter,
        )

        return HttpMetricExporter(endpoint=endpoint, headers=headers)
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import (
        OTLPMetricExporter as GrpcMetricExporter,
    )

    return GrpcMetricExporter(endpoint=endpoint, headers=headers)


__all__ = ["build_otlp_exporter", "build_otlp_metric_exporter"]
