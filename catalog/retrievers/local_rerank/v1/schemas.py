"""Config schema for the local_rerank@v1 reranker stage."""

from pydantic import BaseModel, ConfigDict, Field


class LocalRerankStageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_token_length: int = Field(default=2, ge=1)
    """Query/document tokens shorter than this are ignored when scoring —
    filters bare stop-word noise ("a", "I") without a stop-word list."""
