"""Factory for episode_store@v1: a BM25 episode corpus with ingest().

The corpus loads LAZILY on first retrieve, so a missing/corrupt episode
file surfaces as a RetrievalError at read time — which is exactly what the
episodic memory layer's degrade-gracefully / fail-strict behaviour keys
off (docs/26 § Failure modes)."""

import json
from typing import Any

from foundry.core import RetrievedDocument
from foundry.core.errors import RetrievalError
from foundry.retrieval import BM25Index, RetrieverBuildContext, SparseRetriever


async def build_retriever(
    config,  # EpisodeStoreConfig instance (validated by the wiring)
    ctx: RetrieverBuildContext,
) -> Any:
    corpus_path = ctx.project_dir / config.corpus_path

    class EpisodeStore:
        kind = "sparse"

        def __init__(self) -> None:
            self.name = f"{ctx.slot}:episode_store"
            self._index: BM25Index | None = None
            self._inner: SparseRetriever | None = None
            self._ingested = 0

        def _ensure(self) -> SparseRetriever:
            if self._inner is None:
                if not corpus_path.exists():
                    raise RetrievalError(
                        f"episode corpus not found: {corpus_path}",
                        context={"corpus_path": str(corpus_path)},
                    )
                index = BM25Index()
                for episode in json.loads(corpus_path.read_text()):
                    index.add(
                        episode["id"],
                        episode["text"],
                        source=episode.get("source"),
                        metadata=episode.get("metadata") or {},
                    )
                self._index = index
                self._inner = SparseRetriever(
                    self.name, index.search,
                    emit=ctx.emit, agent_name=ctx.agent_name,
                )
            return self._inner

        async def retrieve(
            self,
            query: str,
            top_k: int = 20,
            filters: dict[str, Any] | None = None,
        ) -> list[RetrievedDocument]:
            return await self._ensure().retrieve(query, top_k, filters)

        async def ingest(self, texts: list[str]) -> None:
            """Add completed turns to the corpus (episodic memory writes)."""
            self._ensure()
            assert self._index is not None
            for text in texts:
                self._ingested += 1
                self._index.add(
                    f"turn-{self._ingested:03d}", text, source="this-session"
                )

    return EpisodeStore()
