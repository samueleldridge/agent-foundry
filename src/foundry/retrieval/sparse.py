"""SparseRetriever: lexical matching (docs/25 § Sparse retrieval), plus a
dependency-free BM25 index for in-process/no-infra retrievers."""

from __future__ import annotations

import math
import re
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from foundry.core import RetrievalEvent, RetrievedDocument
from foundry.core.errors import RetrievalError
from foundry.core.tool import EmitFn

SparseSearchFn = Callable[
    [str, int, dict[str, Any] | None],
    Awaitable[list[RetrievedDocument]],
]
"""(query, top_k, filters) -> ranked documents (BM25 / vendor sparse)."""

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


class SparseRetriever:
    """Exact-term matching: names, IDs, acronyms (docs/25)."""

    kind: Literal["dense", "sparse", "hybrid"] = "sparse"

    def __init__(
        self,
        name: str,
        search: SparseSearchFn,
        *,
        emit: EmitFn | None = None,
        agent_name: str = "",
    ) -> None:
        self.name = name
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
        try:
            documents = await self._search(query, top_k, filters)
        except RetrievalError:
            raise
        except Exception as exc:
            raise RetrievalError(
                f"sparse retriever {self.name!r} search failed: {exc}",
                context={"retriever": self.name, "cause_type": type(exc).__name__},
                cause=exc,
            ) from exc
        documents = documents[:top_k]
        if self._emit is not None:
            self._emit(
                RetrievalEvent,
                agent_name=self._agent_name,
                retriever=self.name,
                kind="sparse",
                top_k=top_k,
                returned=len(documents),
                latency_ms=int((time.monotonic() - started) * 1000),
            )
        return documents


class BM25Index:
    """Okapi BM25 (k1=1.5, b=0.75) over an in-memory corpus. Dev-scale;
    production sparse retrieval binds Elasticsearch/OpenSearch connections."""

    def __init__(self, k1: float = 1.5, b: float = 0.75) -> None:
        self._k1 = k1
        self._b = b
        self._docs: list[tuple[str, str, str | None, dict[str, Any],
                               Counter[str], int]] = []
        self._doc_freq: Counter[str] = Counter()

    def add(
        self,
        doc_id: str,
        text: str,
        *,
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        tokens = _tokenize(text)
        counts = Counter(tokens)
        self._docs.append(
            (doc_id, text, source, metadata or {}, counts, len(tokens))
        )
        for term in counts:
            self._doc_freq[term] += 1

    def __len__(self) -> int:
        return len(self._docs)

    async def search(
        self,
        query: str,
        top_k: int,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedDocument]:
        if not self._docs:
            return []
        n = len(self._docs)
        avg_len = sum(length for *_, length in self._docs) / n
        query_terms = _tokenize(query)
        scored: list[RetrievedDocument] = []
        for doc_id, text, source, metadata, counts, length in self._docs:
            if filters and not all(metadata.get(k) == v for k, v in filters.items()):
                continue
            score = 0.0
            for term in query_terms:
                tf = counts.get(term, 0)
                if tf == 0:
                    continue
                df = self._doc_freq[term]
                idf = math.log(1 + (n - df + 0.5) / (df + 0.5))
                denominator = tf + self._k1 * (
                    1 - self._b + self._b * length / avg_len
                )
                score += idf * (tf * (self._k1 + 1)) / denominator
            if score > 0.0:
                scored.append(
                    RetrievedDocument(
                        id=doc_id, text=text, score=score,
                        source=source, metadata=metadata,
                    )
                )
        scored.sort(key=lambda d: d.score, reverse=True)
        return scored[:top_k]


__all__ = ["BM25Index", "SparseRetriever", "SparseSearchFn"]
