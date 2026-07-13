"""Opt-in Langfuse exporter (docs/80 § Langfuse).

Langfuse ingests OTLP at ``/api/public/otel/v1/traces`` with basic auth
(public key : secret key), so this adapter needs no Langfuse SDK.

    FOUNDRY_TRACING=langfuse
    LANGFUSE_HOST=https://cloud.langfuse.com
    LANGFUSE_PUBLIC_KEY=pk-...
    LANGFUSE_SECRET_KEY=sk-...
"""

from __future__ import annotations

import base64
import os

from opentelemetry.sdk.trace.export import SpanExporter

from foundry.core.errors import ConfigError


def build_langfuse_exporter() -> SpanExporter:
    public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "").strip()
    if not public_key or not secret_key:
        raise ConfigError(
            "FOUNDRY_TRACING=langfuse requires LANGFUSE_PUBLIC_KEY and "
            "LANGFUSE_SECRET_KEY",
            context={"missing_env": "LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY"},
        )
    host = os.environ.get("LANGFUSE_HOST", "https://cloud.langfuse.com").rstrip("/")
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(
        endpoint=f"{host}/api/public/otel/v1/traces",
        headers={"Authorization": f"Basic {token}"},
    )


__all__ = ["build_langfuse_exporter"]
