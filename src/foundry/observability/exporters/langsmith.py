"""Opt-in LangSmith exporter (docs/80 § LangSmith).

LangSmith ingests OTLP directly, so this adapter needs no LangSmith SDK:
it is an OTLP/HTTP span exporter pointed at the LangSmith OTel endpoint
with the API key + project headers.

    FOUNDRY_TRACING=langsmith
    LANGSMITH_API_KEY=...
    LANGSMITH_PROJECT=<project>          (optional)
    LANGSMITH_ENDPOINT=https://api.smith.langchain.com   (optional override)
"""

from __future__ import annotations

import os

from opentelemetry.sdk.trace.export import SpanExporter

from foundry.core.errors import ConfigError


def build_langsmith_exporter() -> SpanExporter:
    api_key = os.environ.get("LANGSMITH_API_KEY", "").strip()
    if not api_key:
        raise ConfigError(
            "FOUNDRY_TRACING=langsmith requires LANGSMITH_API_KEY",
            context={"missing_env": "LANGSMITH_API_KEY"},
        )
    base = os.environ.get("LANGSMITH_ENDPOINT", "https://api.smith.langchain.com").rstrip("/")
    headers = {"x-api-key": api_key}
    project = os.environ.get("LANGSMITH_PROJECT", "").strip()
    if project:
        headers["Langsmith-Project"] = project

    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

    return OTLPSpanExporter(endpoint=f"{base}/otel/v1/traces", headers=headers)


__all__ = ["build_langsmith_exporter"]
