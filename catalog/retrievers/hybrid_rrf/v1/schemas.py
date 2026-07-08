"""Config schema for hybrid_rrf@v1."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SubRetriever(BaseModel):
    """One branch: a nested retriever binding (docs/25 § hybrid_rrf —
    'dense retriever slot + sparse retriever slot, RRF fusion')."""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(pattern=r"^(catalog|local)/[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^v\d+$")
    config: dict[str, Any] = Field(default_factory=dict)
    connection_bindings: dict[str, str] = Field(default_factory=dict)


class HybridRRFConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dense: SubRetriever
    sparse: SubRetriever
    rrf_k: int = Field(default=60, ge=1, le=1000)
