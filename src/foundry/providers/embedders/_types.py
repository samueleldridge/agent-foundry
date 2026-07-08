"""EmbedderBinding + EmbedderSettings (docs/11 § Embedders).

Same pattern as ``ModelBinding`` — provider-agnostic, pluggable, compile-time
validated. ``EmbedderBinding`` appears in ``AgentSpec.semantic_cache`` and in
dense-retriever configs; ``foundry.config.schemas`` re-exports it.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from foundry.core import CredentialsRef, RetryPolicy


class EmbedderSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    batch_size: int = Field(default=64, ge=1, le=2048)
    """Client-side batch size; clamped to the model's max_batch_size."""
    timeout_s: float = Field(default=30.0, gt=0)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)


class EmbedderBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    """Canonical embedder provider: 'voyage', 'openai', 'cohere', 'bedrock'."""
    model: str
    """Model id, e.g. 'voyage-3', 'text-embedding-3-small',
    'embed-english-v3.0'."""
    settings: EmbedderSettings = Field(default_factory=EmbedderSettings)
    credentials_ref: CredentialsRef | None = None


__all__ = ["EmbedderBinding", "EmbedderSettings"]
