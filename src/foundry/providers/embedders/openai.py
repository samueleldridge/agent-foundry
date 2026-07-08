"""OpenAI embedder adapter (docs/11 § Concrete embedders).

Symmetric embeddings only — ``purpose`` is accepted and ignored (the
capabilities record advertises ``supports_query_document_split: False``).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

from foundry.core import EmbedderCapabilities, EmbedderPricing
from foundry.providers._base import HttpRequestSpec
from foundry.providers.embedders._base import (
    EmbedderAdapter,
    ParsedEmbedBatch,
    Purpose,
)
from foundry.providers.embedders._registry import register_embedder

_BASE_URL = "https://api.openai.com/v1/embeddings"


def _caps(model: str, dimensions: int, price: str) -> EmbedderCapabilities:
    return EmbedderCapabilities(
        provider="openai",
        model=model,
        dimensions=dimensions,
        max_input_tokens=8_191,
        supports_query_document_split=False,
        supports_batch=True,
        max_batch_size=2_048,
        pricing=EmbedderPricing(input_per_1m=Decimal(price)),
    )


OPENAI_MODELS: dict[str, EmbedderCapabilities] = {
    "text-embedding-3-small": _caps("text-embedding-3-small", 1536, "0.02"),
    "text-embedding-3-large": _caps("text-embedding-3-large", 3072, "0.13"),
}


@register_embedder("openai", OPENAI_MODELS)
class OpenAIEmbedder(EmbedderAdapter):
    provider_name: ClassVar[str] = "openai"
    default_credentials_env: ClassVar[str] = "OPENAI_API_KEY"

    def _build_request(self, texts: list[str], purpose: Purpose) -> HttpRequestSpec:
        _ = purpose  # symmetric embedder: query == document
        return HttpRequestSpec(
            url=_BASE_URL,
            headers={"Authorization": f"Bearer {self._api_key()}"},
            body={"model": self.model, "input": texts},
        )

    def _parse_response(self, payload: dict[str, Any]) -> ParsedEmbedBatch:
        data = sorted(payload["data"], key=lambda item: item["index"])
        usage = payload.get("usage") or {}
        return ParsedEmbedBatch(
            vectors=[item["embedding"] for item in data],
            input_tokens=int(usage.get("prompt_tokens", 0)),
        )


__all__ = ["OPENAI_MODELS", "OpenAIEmbedder"]
