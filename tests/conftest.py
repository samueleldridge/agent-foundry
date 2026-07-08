"""Shared fixtures.

The OpenTelemetry tracer provider is process-global and installable only
once, so every test that asserts on spans shares this exporter: the fixture
installs an SDK ``TracerProvider`` with an in-memory exporter on first use
and clears captured spans before each test that requests it.
"""

from __future__ import annotations

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

_EXPORTER = InMemorySpanExporter()
_INSTALLED = False


@pytest.fixture
def span_exporter() -> InMemorySpanExporter:
    global _INSTALLED
    if not _INSTALLED:
        provider = TracerProvider()
        provider.add_span_processor(SimpleSpanProcessor(_EXPORTER))
        trace.set_tracer_provider(provider)
        _INSTALLED = True
    _EXPORTER.clear()
    return _EXPORTER
