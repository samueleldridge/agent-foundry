"""Voyage embedder adapter (docs/11 § Concrete embedders).

Recommended partner for Anthropic deployments. Supports asymmetric
query/document embeddings via the ``input_type`` request field.
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

_BASE_URL = "https://api.voyageai.com/v1/embeddings"


def _caps(model: str, dimensions: int, price: str) -> EmbedderCapabilities:
    return EmbedderCapabilities(
        provider="voyage",
        model=model,
        dimensions=dimensions,
        max_input_tokens=32_000,
        supports_query_document_split=True,
        supports_batch=True,
        max_batch_size=128,
        pricing=EmbedderPricing(input_per_1m=Decimal(price)),
    )


VOYAGE_MODELS: dict[str, EmbedderCapabilities] = {
    "voyage-3": _caps("voyage-3", 1024, "0.06"),
    "voyage-3-large": _caps("voyage-3-large", 1024, "0.18"),
    "voyage-code-3": _caps("voyage-code-3", 1024, "0.18"),
}


@register_embedder("voyage", VOYAGE_MODELS)
class VoyageEmbedder(EmbedderAdapter):
    provider_name: ClassVar[str] = "voyage"
    default_credentials_env: ClassVar[str] = "VOYAGE_API_KEY"

    def _build_request(self, texts: list[str], purpose: Purpose) -> HttpRequestSpec:
        return HttpRequestSpec(
            url=_BASE_URL,
            headers={"Authorization": f"Bearer {self._api_key()}"},
            body={
                "model": self.model,
                "input": texts,
                "input_type": purpose,  # 'query' | 'document' — asymmetric
            },
        )

    def _parse_response(self, payload: dict[str, Any]) -> ParsedEmbedBatch:
        data = sorted(payload["data"], key=lambda item: item["index"])
        usage = payload.get("usage") or {}
        return ParsedEmbedBatch(
            vectors=[item["embedding"] for item in data],
            input_tokens=int(usage.get("total_tokens", 0)),
        )


__all__ = ["VOYAGE_MODELS", "VoyageEmbedder"]
