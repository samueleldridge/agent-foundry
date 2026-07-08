"""Factory for docs_dense@v1: DenseRetriever over an in-memory index built
lazily from corpus.json (first retrieve embeds the corpus once)."""

import json
from typing import Any

from foundry.core import RetrievedDocument
from foundry.core.errors import RetrievalError
from foundry.retrieval import (
    DenseRetriever,
    InMemoryVectorIndex,
    RetrieverBuildContext,
)


async def build_retriever(
    config,  # DocsDenseConfig instance (validated by the wiring)
    ctx: RetrieverBuildContext,
) -> DenseRetriever:
    assert ctx.embedder is not None  # config declares the binding
    corpus_path = ctx.project_dir / config.corpus_path
    embedder = ctx.embedder
    holder: dict[str, InMemoryVectorIndex] = {}

    async def search(
        vector: list[float], top_k: int, filters: dict[str, Any] | None
    ) -> list[RetrievedDocument]:
        index = holder.get("index")
        if index is None:
            if not corpus_path.exists():
                raise RetrievalError(
                    f"docs_dense corpus not found: {corpus_path}",
                    context={"corpus_path": str(corpus_path)},
                )
            documents = json.loads(corpus_path.read_text())
            embeddings = await embedder.embed(
                [d["text"] for d in documents], "document"
            )
            index = InMemoryVectorIndex()
            for document, embedding in zip(documents, embeddings, strict=True):
                index.add(
                    document["id"],
                    document["text"],
                    embedding.vector,
                    source=document.get("source"),
                    metadata=document.get("metadata") or {},
                )
            holder["index"] = index
        return await index.search(vector, top_k, filters)

    return DenseRetriever(
        f"{ctx.slot}:docs_dense",
        embedder,
        search,
        emit=ctx.emit,
        agent_name=ctx.agent_name,
    )
