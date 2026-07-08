"""Config schema for the cohere_rerank@v1 reranker stage."""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class CohereRerankStageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    price_per_call_usd: Decimal = Field(
        default=Decimal("0.002"), ge=0
    )
    """Cost attributed per rerank call ($2 per 1k searches at list price);
    pin your negotiated tier here. Feeds the rerank event's
    cost_estimate_usd (docs/25 § Cost-aware use)."""
    model: str = "rerank-english-v3.0"
    """Fallback model name when the bound connection does not expose one;
    the connection's pinned model wins when present."""
