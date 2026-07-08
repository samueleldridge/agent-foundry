"""Factory for docs_sparse@v1: SparseRetriever over a lazily-built BM25
index from corpus.json."""

import json
from typing import Any

from foundry.core import RetrievedDocument
from foundry.core.errors import RetrievalError
from foundry.retrieval import BM25Index, RetrieverBuildContext, SparseRetriever


async def build_retriever(
    config,  # DocsSparseConfig instance (validated by the wiring)
    ctx: RetrieverBuildContext,
) -> SparseRetriever:
    corpus_path = ctx.project_dir / config.corpus_path
    holder: dict[str, BM25Index] = {}

    async def search(
        query: str, top_k: int, filters: dict[str, Any] | None
    ) -> list[RetrievedDocument]:
        index = holder.get("index")
        if index is None:
            if not corpus_path.exists():
                raise RetrievalError(
                    f"docs_sparse corpus not found: {corpus_path}",
                    context={"corpus_path": str(corpus_path)},
                )
            index = BM25Index()
            for document in json.loads(corpus_path.read_text()):
                index.add(
                    document["id"],
                    document["text"],
                    source=document.get("source"),
                    metadata=document.get("metadata") or {},
                )
            holder["index"] = index
        return await index.search(query, top_k, filters)

    return SparseRetriever(
        f"{ctx.slot}:docs_sparse",
        search,
        emit=ctx.emit,
        agent_name=ctx.agent_name,
    )
