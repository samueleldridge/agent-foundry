"""Embedder protocol — Phase 1 type stub.

Concrete embedders + semantic cache integration land in Phase 2b. Phase 1
ships the protocol shape so config schemas (EmbedderBinding) and the public
``core`` re-export are stable. See docs/10 § Embeddings.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class EmbedderPricing(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_1m: Decimal


class EmbedderCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    dimensions: int
    max_input_tokens: int
    supports_query_document_split: bool = False
    supports_batch: bool = True
    max_batch_size: int = 128
    pricing: EmbedderPricing

    def dim_matches(self, other: EmbedderCapabilities) -> bool:
        return self.dimensions == other.dimensions


class Embedding(BaseModel):
    model_config = ConfigDict(frozen=True)

    vector: list[float]
    dimensions: int
    model: str
    input_tokens: int
    latency_ms: int
    cost_estimate_usd: Decimal | None = None


@runtime_checkable
class Embedder(Protocol):
    """Property-style members so implementations may use attributes OR
    read-only properties (EmbedderAdapter derives ``name`` from
    provider+model)."""

    @property
    def name(self) -> str: ...

    @property
    def model(self) -> str: ...

    @property
    def capabilities(self) -> EmbedderCapabilities: ...

    async def embed(
        self,
        inputs: list[str],
        purpose: Literal["query", "document"] = "document",
    ) -> list[Embedding]: ...


__all__ = ["Embedder", "EmbedderCapabilities", "EmbedderPricing", "Embedding"]
