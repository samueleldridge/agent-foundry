"""Config schema for cohere_rerank@v1."""

from pydantic import BaseModel, ConfigDict, Field


class CohereRerankConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(default="https://api.cohere.com", pattern=r"^https?://")
    model: str = "rerank-english-v3.0"
    timeout_s: float = Field(default=15.0, gt=0)
