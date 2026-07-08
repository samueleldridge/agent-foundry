"""Retriever + Reranker protocols and RetrievedDocument (docs/10, docs/25).

Concrete retrievers/rerankers live in ``foundry.retrieval``; catalog/project
retriever factories build against these protocols. Tool handlers reach
retrievers via ``ctx.retrievers`` (the ``RetrieverAccessor``).
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


@runtime_checkable
class RetrieverAccessor(Protocol):
    """Slot-name → Retriever, parallel to ``ctx.connections``
    (docs/25 § RetrieverBinding). Unknown slot raises ``RetrievalError``."""

    def get(self, slot: str) -> Retriever: ...


__all__ = ["Reranker", "RetrievedDocument", "Retriever", "RetrieverAccessor"]
