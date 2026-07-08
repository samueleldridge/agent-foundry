"""Shared plumbing for HTTP reranker adapters (docs/25 § Rerankers).

Contract (docs/25): input order is ignored; output ``score`` is replaced by
the reranker's relevance score; truncation to ``top_k`` when given; id /
text / source / metadata preserved. Every call emits a ``rerank`` event with
``cost_estimate_usd`` ALWAYS populated — a defensive Decimal('0') when the
adapter cannot compute a real figure (exit-gate requirement), never None.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

import httpx

from foundry.core import RerankEvent, RetrievedDocument
from foundry.core.errors import RerankError
from foundry.core.tool import EmitFn

ClientProvider = Callable[[], Awaitable[httpx.AsyncClient]]
"""Async supplier of an authenticated httpx client — typically a closure over
``ctx.connections.get(<slot>)`` so the reranker rides the pooled connection."""


class HTTPReranker(ABC):
    """Base for Cohere / Voyage / Jina adapters."""

    def __init__(
        self,
        name: str,
        model: str,
        get_client: ClientProvider,
        *,
        emit: EmitFn | None = None,
        agent_name: str = "",
    ) -> None:
        self.name = name
        self.model = model
        self._get_client = get_client
        self._emit = emit
        self._agent_name = agent_name

    @abstractmethod
    def _request_path(self) -> str: ...

    @abstractmethod
    def _request_body(
        self, query: str, documents: list[RetrievedDocument], top_k: int | None
    ) -> dict[str, Any]: ...

    @abstractmethod
    def _parse_results(self, payload: dict[str, Any]) -> list[tuple[int, float]]:
        """→ [(input_index, relevance_score)] best-first."""

    def _cost_estimate(self, payload: dict[str, Any]) -> Decimal | None:
        """Adapter-specific cost; None → the defensive default applies."""
        _ = payload
        return None

    async def rerank(
        self,
        query: str,
        documents: list[RetrievedDocument],
        top_k: int | None = None,
    ) -> list[RetrievedDocument]:
        if not documents:
            return []
        started = time.monotonic()
        client = await self._get_client()
        try:
            response = await client.post(
                self._request_path(),
                json=self._request_body(query, documents, top_k),
            )
        except httpx.HTTPError as exc:
            raise RerankError(
                f"reranker {self.name!r} transport error: {exc}",
                context={"reranker": self.name, "model": self.model},
                cause=exc,
            ) from exc
        if response.status_code >= 400:
            raise RerankError(
                f"reranker {self.name!r} call failed "
                f"(HTTP {response.status_code}): {response.text[:300]}",
                context={"reranker": self.name, "model": self.model,
                         "http_status": response.status_code},
            )
        try:
            payload = response.json()
            results = self._parse_results(payload)
        except Exception as exc:
            raise RerankError(
                f"reranker {self.name!r} returned an unparseable response: {exc}",
                context={"reranker": self.name, "model": self.model},
                cause=exc,
            ) from exc

        reranked: list[RetrievedDocument] = []
        for index, score in results:
            if not 0 <= index < len(documents):
                raise RerankError(
                    f"reranker {self.name!r} returned out-of-range index {index}",
                    context={"reranker": self.name, "index": index,
                             "candidates": len(documents)},
                )
            original = documents[index]
            # Invariant (docs/25): rerankers never introduce documents —
            # new instance, new score, same id/text/source/metadata.
            reranked.append(
                RetrievedDocument(
                    id=original.id,
                    text=original.text,
                    score=score,
                    source=original.source,
                    metadata=original.metadata,
                )
            )
        if top_k is not None:
            reranked = reranked[:top_k]

        # Defensive default: cost_estimate_usd is ALWAYS a Decimal.
        cost = self._cost_estimate(payload)
        if cost is None:
            cost = Decimal("0")
        if self._emit is not None:
            self._emit(
                RerankEvent,
                agent_name=self._agent_name,
                reranker=f"{self.name}:{self.model}",
                candidates=len(documents),
                top_k=top_k,
                latency_ms=int((time.monotonic() - started) * 1000),
                cost_estimate_usd=cost,
                before_ids=[d.id for d in documents],
                after_ids=[d.id for d in reranked],
            )
        return reranked


__all__ = ["ClientProvider", "HTTPReranker"]
