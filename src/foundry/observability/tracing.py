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


__all__ = [
    "foundry_span",
    "set_span_attributes",
    "worker_id",
]
