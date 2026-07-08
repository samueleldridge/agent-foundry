"""HybridRetriever: dense + sparse in parallel, merged via Reciprocal Rank
Fusion (docs/25 § Hybrid retrieval).

RRF: ``score(doc) = sum over branches of 1 / (k + rank)`` with 1-based ranks
and k=60 by default — rank-based, parameter-free, no score normalisation.

Fail-degradation rule (docs/25 § Failure modes): one branch failing falls
through to the other with a warning event; BOTH failing raises
``RetrievalError``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Literal

from foundry.core import (
    RetrievalEvent,
    RetrievedDocument,
    Retriever,
    WarningEvent,
)
from foundry.core.errors import FoundryError, RetrievalError
from foundry.core.tool import EmitFn

DEFAULT_RRF_K = 60


def rrf_merge(
    ranked_lists: dict[str, list[RetrievedDocument]],
    *,
    k: int = DEFAULT_RRF_K,
) -> list[RetrievedDocument]:
    """Merge branch results by RRF. Deduplicates by document id; text/source/
    metadata come from the first branch (in dict order) that returned the doc.
    Returned ``score`` is the RRF score (docs/25: cross-retriever comparisons
    are only meaningful via rank-based fusion)."""
    scores: dict[str, float] = {}
    first_seen: dict[str, RetrievedDocument] = {}
    for documents in ranked_lists.values():
        for rank, document in enumerate(documents, start=1):
            scores[document.id] = scores.get(document.id, 0.0) + 1.0 / (k + rank)
            first_seen.setdefault(document.id, document)
    merged = [
        RetrievedDocument(
            id=doc_id,
            text=first_seen[doc_id].text,
            score=score,
            source=first_seen[doc_id].source,
            metadata=first_seen[doc_id].metadata,
        )
        for doc_id, score in scores.items()
    ]
    merged.sort(key=lambda d: d.score, reverse=True)
    return merged


class HybridRetriever:
    kind: Literal["dense", "sparse", "hybrid"] = "hybrid"

    def __init__(
        self,
        name: str,
        dense: Retriever,
        sparse: Retriever,
        *,
        rrf_k: int = DEFAULT_RRF_K,
        emit: EmitFn | None = None,
        agent_name: str = "",
    ) -> None:
        self.name = name
        self.dense = dense
        self.sparse = sparse
        self.rrf_k = rrf_k
        self._emit = emit
        self._agent_name = agent_name

    async def retrieve(
        self,
        query: str,
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedDocument]:
        started = time.monotonic()

        async def _timed(
            branch: Retriever,
        ) -> tuple[list[RetrievedDocument], int]:
            branch_started = time.monotonic()
            documents = await branch.retrieve(query, top_k, filters)
            return documents, int((time.monotonic() - branch_started) * 1000)

        # Both branches run in parallel (exit gate: dense + sparse fan-out).
        dense_result, sparse_result = await asyncio.gather(
            _timed(self.dense), _timed(self.sparse), return_exceptions=True
        )

        ranked: dict[str, list[RetrievedDocument]] = {}
        branch_latency_ms: dict[str, int] = {}
        failed: list[str] = []
        failures: dict[str, BaseException] = {}
        for branch_name, result in (
            ("dense", dense_result),
            ("sparse", sparse_result),
        ):
            if isinstance(result, BaseException):
                if not isinstance(result, FoundryError):
                    raise result  # cancellation etc. — never swallow
                failed.append(branch_name)
                failures[branch_name] = result
                if self._emit is not None:
                    self._emit(
                        WarningEvent,
                        agent_name=self._agent_name,
                        category="retrieval.branch_failed",
                        message=f"hybrid retriever {self.name!r} branch "
                        f"{branch_name!r} failed; degrading to the other "
                        f"branch: {result}",
                        error_class=type(result).__name__,
                    )
            else:
                documents, latency = result
                ranked[branch_name] = documents
                branch_latency_ms[branch_name] = latency

        if not ranked:
            raise RetrievalError(
                f"hybrid retriever {self.name!r}: both branches failed "
                f"(dense: {failures.get('dense')}; "
                f"sparse: {failures.get('sparse')})",
                context={
                    "retriever": self.name,
                    "branches_failed": failed,
                },
            )

        merged = rrf_merge(ranked, k=self.rrf_k)[:top_k]
        if self._emit is not None:
            self._emit(
                RetrievalEvent,
                agent_name=self._agent_name,
                retriever=self.name,
                kind="hybrid",
                top_k=top_k,
                returned=len(merged),
                latency_ms=int((time.monotonic() - started) * 1000),
                branch_latency_ms=branch_latency_ms,
                branches_failed=failed,
            )
        return merged


__all__ = ["DEFAULT_RRF_K", "HybridRetriever", "rrf_merge"]
