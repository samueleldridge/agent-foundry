"""DenseRetriever: Embedder + vector search (docs/25 § Dense retrieval),
plus the in-memory vector index the no-infra example projects build on.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from foundry.cache.keys import cosine_similarity
from foundry.core import EmbedCall, Embedder, RetrievalEvent, RetrievedDocument
from foundry.core.errors import RetrievalError
from foundry.core.tool import EmitFn

VectorSearchFn = Callable[
    [list[float], int, dict[str, Any] | None],
    Awaitable[list[RetrievedDocument]],
]
"""(query_vector, top_k, filters) -> ranked documents. Backing-store-specific;
supplied by the retriever factory (pgvector SQL, in-memory index, ...)."""


class DenseRetriever:
    """Semantic similarity via embeddings: embed the query with
    purpose='query', then search the vector store (docs/25)."""

    kind: Literal["dense", "sparse", "hybrid"] = "dense"

    def __init__(
        self,
        name: str,
        embedder: Embedder,
        search: VectorSearchFn,
        *,
        emit: EmitFn | None = None,
        agent_name: str = "",
    ) -> None:
        self.name = name
        self.embedder = embedder
        self._search = search
        self._emit = emit
        self._agent_name = agent_name

    async def retrieve(
        self,
        query: str,
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedDocument]:
        started = time.monotonic()
        embeddings = await self.embedder.embed([query], "query")
        embedding = embeddings[0]
        if self._emit is not None:
            self._emit(
                EmbedCall,
                agent_name=self._agent_name,
                embedder=self.embedder.name,
                input_count=1,
                input_tokens=embedding.input_tokens,
                purpose="query",
                latency_ms=embedding.latency_ms,
                cost_estimate_usd=embedding.cost_estimate_usd,
            )
        try:
            documents = await self._search(embedding.vector, top_k, filters)
        except RetrievalError:
            raise
        except Exception as exc:
            raise RetrievalError(
                f"dense retriever {self.name!r} search failed: {exc}",
                context={"retriever": self.name, "cause_type": type(exc).__name__},
                cause=exc,
            ) from exc
        documents = documents[:top_k]
        if self._emit is not None:
            self._emit(
                RetrievalEvent,
                agent_name=self._agent_name,
                retriever=self.name,
                kind="dense",
                top_k=top_k,
                returned=len(documents),
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        return documents


class InMemoryVectorIndex:
    """Brute-force cosine index for in-process/no-infra retrievers (the same
    posture as the in_process cache backend: dev-scale, dependency-free)."""

    def __init__(self) -> None:
        self._entries: list[tuple[str, str, list[float], str | None,
                                  dict[str, Any]]] = []

    def add(
        self,
        doc_id: str,
        text: str,
        vector: list[float],
        *,
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self._entries.append((doc_id, text, vector, source, metadata or {}))

    def __len__(self) -> int:
        return len(self._entries)

    async def search(
        self,
        vector: list[float],
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedDocument]:
        scored: list[RetrievedDocument] = []
        for doc_id, text, doc_vector, source, metadata in self._entries:
            if filters and not all(metadata.get(k) == v for k, v in filters.items()):
                continue
            scored.append(
                RetrievedDocument(
                    id=doc_id,
                    text=text,
                    score=cosine_similarity(vector, doc_vector),
                    source=source,
                    metadata=metadata,
                )
            )
        scored.sort(key=lambda d: d.score, reverse=True)
        return scored[:top_k]


__all__ = ["DenseRetriever", "InMemoryVectorIndex", "VectorSearchFn"]
