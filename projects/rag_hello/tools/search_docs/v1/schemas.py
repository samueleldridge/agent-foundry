"""Schemas for search_docs@v1."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class SearchIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=50)
    """Override the binding's default top_k for this call."""


class SearchOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    documents: list[dict[str, Any]]
    """RetrievedDocument dumps: id, text, score, source, metadata."""
