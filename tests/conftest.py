"""Shared fixtures.

The OpenTelemetry tracer/meter providers are process-global and installable
only once, so every test that asserts on spans/metrics shares these
exporters: the fixtures install SDK providers with in-memory exporters on
first use and clear captured data before each test that requests them.

``_isolated_foundry_home`` (autouse) keeps any test that forgot to set
``FOUNDRY_HOME`` from writing run artifacts or the Phase 9 observability
mirror into the developer's real ``~/.foundry``.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator

import pytest
from opentelemetry import metrics as otel_metrics
from opentelemetry import trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

_EXPORTER = InMemorySpanExporter()
_INSTALLED = False

_METRIC_READER: InMemoryMetricReader | None = None


@pytest.fixture(autouse=True)
def _isolated_foundry_home(
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Point FOUNDRY_HOME at a temp dir unless the test sets its own, and
    reset the observability dispatcher's cached store/correlation state so
    per-test homes never bleed into each other."""
    from foundry.observability.events import reset_dispatcher

    if "FOUNDRY_HOME" not in os.environ:
        home = tmp_path_factory.mktemp("foundry_home_default")
        monkeypatch.setenv("FOUNDRY_HOME", str(home))
    reset_dispatcher()
    yield
    reset_dispatcher()


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


@pytest.fixture
def metric_reader() -> InMemoryMetricReader:
    """Process-global in-memory metric reader. NOTE: OTel counters are
    cumulative — tests should diff or scope assertions to their own
    dimension values (e.g. a unique project name) rather than expect a
    clean slate."""
    global _METRIC_READER
    if _METRIC_READER is None:
        _METRIC_READER = InMemoryMetricReader()
        otel_metrics.set_meter_provider(MeterProvider(metric_readers=[_METRIC_READER]))

        from foundry.observability.metrics import reset_metrics_recorder

        reset_metrics_recorder()
    return _METRIC_READER


@pytest.fixture
def read_metric_points_fn() -> (
    Callable[[InMemoryMetricReader, str], list[tuple[dict[str, object], float]]]
):
    """Fixture wrapper so tests can use the helper without importing
    conftest (tests/ is not a package)."""
    return read_metric_points


def read_metric_points(
    reader: InMemoryMetricReader, name: str
) -> list[tuple[dict[str, object], float]]:
    """Helper for tests: flatten (attributes, value) pairs for one metric."""
    data = reader.get_metrics_data()
    points: list[tuple[dict[str, object], float]] = []
    if data is None:
        return points
    for resource_metric in data.resource_metrics:
        for scope_metric in resource_metric.scope_metrics:
            for metric in scope_metric.metrics:
                if metric.name != name:
                    continue
                for point in metric.data.data_points:
                    value = getattr(point, "value", None)
                    if value is None:
                        value = getattr(point, "sum", 0.0)
                    points.append((dict(point.attributes or {}), float(value)))
    return points
