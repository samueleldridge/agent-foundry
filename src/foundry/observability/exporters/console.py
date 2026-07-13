"""Dev-only stdout exporter (docs/80 module layout)."""

from __future__ import annotations

from opentelemetry.sdk.trace.export import ConsoleSpanExporter, SpanExporter


def build_console_exporter() -> SpanExporter:
    return ConsoleSpanExporter()


__all__ = ["build_console_exporter"]
