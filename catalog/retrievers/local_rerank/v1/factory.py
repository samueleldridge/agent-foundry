"""Factory for local_rerank@v1: deterministic lexical-overlap reranker.

Self-contained on purpose (docs/25 § Rerankers): the framework's
``LocalCrossEncoderReranker`` is a fail-loud stub pending a real serving
endpoint, while this catalog stage is genuinely runnable — no model
download, no connection, no key. Scoring is coverage of the query's
tokens by the document's tokens; ties keep the incoming order, so the
stage is stable and fully reproducible.
"""

import re
import time
from decimal import Decimal

from foundry.core import RerankEvent, RetrievedDocument
from foundry.retrieval import RetrieverBuildContext

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str, min_len: int) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.lower()) if len(t) >= min_len}


class LexicalOverlapReranker:
    """Reorders by fraction of query tokens present in each document.

    Contract (docs/25): input order ignored (except as tie-break); output
    ``score`` is the overlap score in [0, 1]; truncation to ``top_k`` when
    given; id/text/source/metadata preserved; never introduces documents.
    Emits a ``rerank`` event with ``cost_estimate_usd`` always populated —
    Decimal('0'): local reranking is free.
    """

    name = "local_rerank"
    model = "lexical-overlap"

    def __init__(self, min_token_length, emit=None, agent_name=""):
        self._min_token_length = min_token_length
        self._emit = emit
        self._agent_name = agent_name

    async def rerank(
        self,
        query: str,
        documents: list[RetrievedDocument],
        top_k: int | None = None,
    ) -> list[RetrievedDocument]:
        if not documents:
            return []
        started = time.monotonic()
        query_tokens = _tokens(query, self._min_token_length)
        scored: list[tuple[float, int, RetrievedDocument]] = []
        for index, doc in enumerate(documents):
            if query_tokens:
                doc_tokens = _tokens(doc.text, self._min_token_length)
                score = len(query_tokens & doc_tokens) / len(query_tokens)
            else:
                score = 0.0
            scored.append((score, index, doc))
        # Best score first; equal scores keep the incoming order (stable).
        scored.sort(key=lambda item: (-item[0], item[1]))
        reranked = [
            RetrievedDocument(
                id=doc.id,
                text=doc.text,
                score=score,
                source=doc.source,
                metadata=doc.metadata,
            )
            for score, _, doc in scored
        ]
        if top_k is not None:
            reranked = reranked[:top_k]
        if self._emit is not None:
            self._emit(
                RerankEvent,
                agent_name=self._agent_name,
                reranker=f"{self.name}:{self.model}",
                candidates=len(documents),
                top_k=top_k,
                latency_ms=int((time.monotonic() - started) * 1000),
                cost_estimate_usd=Decimal("0"),
                before_ids=[d.id for d in documents],
                after_ids=[d.id for d in reranked],
            )
        return reranked


async def build_reranker(
    config,  # LocalRerankStageConfig instance (validated by the wiring)
    ctx: RetrieverBuildContext,
) -> LexicalOverlapReranker:
    return LexicalOverlapReranker(
        config.min_token_length,
        emit=ctx.emit,
        agent_name=ctx.agent_name,
    )
