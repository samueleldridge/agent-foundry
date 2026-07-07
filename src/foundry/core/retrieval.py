"""Retriever + Reranker protocols — Phase 1 type stubs.

Concrete retrievers land in Phase 2b. Phase 1 ships protocol shapes so the
public ``core`` re-export is stable. See docs/10 § Retrieval primitives.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class RetrievedDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    text: str
    score: float
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


@runtime_checkable
class Retriever(Protocol):
    name: str
    kind: Literal["dense", "sparse", "hybrid"]

    async def retrieve(
        self,
        query: str,
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedDocument]: ...


@runtime_checkable
class Reranker(Protocol):
    name: str
    model: str

    async def rerank(
        self,
        query: str,
        documents: list[RetrievedDocument],
        top_k: int | None = None,
    ) -> list[RetrievedDocument]: ...


__all__ = ["Reranker", "RetrievedDocument", "Retriever"]
