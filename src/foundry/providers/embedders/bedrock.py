"""Bedrock embedder adapter — registered STUB (documented deviation).

Bedrock's InvokeModel API requires SigV4 request signing, which has no clean
direct-httpx path without pulling in boto3/botocore (not pinned in Phase 0;
the same deferral as the generation-side Bedrock provider and the sigv4
``kind=default`` chain in Phase 2a). The adapter therefore:

- registers, so ``load_embedder`` resolves 'bedrock' bindings and the
  compile-time dimension check works against its advertised capabilities;
- raises a structured ``EmbedderConfigError`` from ``embed()`` naming the
  missing dependency, so a runtime call fails loudly and actionably.

Full implementation lands with the Bedrock generation provider.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, ClassVar

from foundry.core import EmbedderCapabilities, EmbedderPricing, Embedding
from foundry.core.errors import EmbedderConfigError
from foundry.providers._base import HttpRequestSpec
from foundry.providers.embedders._base import (
    EmbedderAdapter,
    ParsedEmbedBatch,
    Purpose,
)
from foundry.providers.embedders._registry import register_embedder


def _caps(
    model: str, dimensions: int, price: str, *, split: bool
) -> EmbedderCapabilities:
    return EmbedderCapabilities(
        provider="bedrock",
        model=model,
        dimensions=dimensions,
        max_input_tokens=8_192,
        supports_query_document_split=split,
        supports_batch=False,
        max_batch_size=1,
        pricing=EmbedderPricing(input_per_1m=Decimal(price)),
    )


BEDROCK_MODELS: dict[str, EmbedderCapabilities] = {
    "amazon.titan-embed-text-v2": _caps(
        "amazon.titan-embed-text-v2", 1024, "0.02", split=False
    ),
    "cohere.embed-english-v3": _caps(
        "cohere.embed-english-v3", 1024, "0.10", split=True
    ),
}


@register_embedder("bedrock", BEDROCK_MODELS)
class BedrockEmbedder(EmbedderAdapter):
    provider_name: ClassVar[str] = "bedrock"
    default_credentials_env: ClassVar[str] = "AWS_PROFILE"

    async def embed(
        self,
        inputs: list[str],
        purpose: Purpose = "document",
    ) -> list[Embedding]:
        raise EmbedderConfigError(
            "the bedrock embedder is a registered stub in Phase 2b: "
            "InvokeModel needs SigV4 signing (boto3/botocore), which is not a "
            "pinned dependency. Bind provider 'voyage', 'openai', or 'cohere' "
            "instead, or wait for the Bedrock provider phase.",
            context={
                "embedder": self.name,
                "missing_dependency": "boto3",
                "alternatives": ["voyage", "openai", "cohere"],
            },
        )

    def _build_request(self, texts: list[str], purpose: Purpose) -> HttpRequestSpec:
        raise NotImplementedError("bedrock embedder stub — see embed()")

    def _parse_response(self, payload: dict[str, Any]) -> ParsedEmbedBatch:
        raise NotImplementedError("bedrock embedder stub — see embed()")


__all__ = ["BEDROCK_MODELS", "BedrockEmbedder"]
