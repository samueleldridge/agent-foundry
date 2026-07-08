"""Cohere embedder adapter (docs/11 § Concrete embedders).

Asymmetric embeddings via ``input_type: search_query | search_document``.
Pairs naturally with Cohere Rerank (docs/25).
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

_BASE_URL = "https://api.cohere.com/v1/embed"

_PURPOSE_TO_INPUT_TYPE: dict[str, str] = {
    "query": "search_query",
    "document": "search_document",
}


def _caps(model: str, dimensions: int, price: str) -> EmbedderCapabilities:
    return EmbedderCapabilities(
        provider="cohere",
        model=model,
        dimensions=dimensions,
        max_input_tokens=512,
        supports_query_document_split=True,
        supports_batch=True,
        max_batch_size=96,
        pricing=EmbedderPricing(input_per_1m=Decimal(price)),
    )


COHERE_MODELS: dict[str, EmbedderCapabilities] = {
    "embed-english-v3.0": _caps("embed-english-v3.0", 1024, "0.10"),
    "embed-multilingual-v3.0": _caps("embed-multilingual-v3.0", 1024, "0.10"),
}


@register_embedder("cohere", COHERE_MODELS)
class CohereEmbedder(EmbedderAdapter):
    provider_name: ClassVar[str] = "cohere"
    default_credentials_env: ClassVar[str] = "COHERE_API_KEY"

    def _build_request(self, texts: list[str], purpose: Purpose) -> HttpRequestSpec:
        return HttpRequestSpec(
            url=_BASE_URL,
            headers={"Authorization": f"Bearer {self._api_key()}"},
            body={
                "model": self.model,
                "texts": texts,
                "input_type": _PURPOSE_TO_INPUT_TYPE[purpose],
            },
        )

    def _parse_response(self, payload: dict[str, Any]) -> ParsedEmbedBatch:
        embeddings = payload["embeddings"]
        if isinstance(embeddings, dict):  # embedding_types-shaped response
            embeddings = embeddings["float"]
        meta = payload.get("meta") or {}
        billed = meta.get("billed_units") or {}
        return ParsedEmbedBatch(
            vectors=list(embeddings),
            input_tokens=int(billed.get("input_tokens", 0)),
        )


__all__ = ["COHERE_MODELS", "CohereEmbedder"]
