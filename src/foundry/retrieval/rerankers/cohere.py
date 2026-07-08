"""Cohere Rerank adapter (docs/25: rerank-3 / rerank-english-v3.0 /
rerank-multilingual-v3.0). Billed per search unit — the per-call price is a
constructor knob so operators can pin their negotiated tier."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from foundry.core import RetrievedDocument
from foundry.core.tool import EmitFn
from foundry.retrieval.rerankers._base import ClientProvider, HTTPReranker

_DEFAULT_PRICE_PER_CALL = Decimal("0.002")  # $2 per 1k rerank searches


class CohereReranker(HTTPReranker):
    def __init__(
        self,
        model: str,
        get_client: ClientProvider,
        *,
        price_per_call_usd: Decimal = _DEFAULT_PRICE_PER_CALL,
        emit: EmitFn | None = None,
        agent_name: str = "",
    ) -> None:
        super().__init__(
            "cohere_rerank", model, get_client, emit=emit, agent_name=agent_name
        )
        self._price_per_call_usd = price_per_call_usd

    def _request_path(self) -> str:
        return "/v1/rerank"

    def _request_body(
        self, query: str, documents: list[RetrievedDocument], top_k: int | None
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": self.model,
            "query": query,
            "documents": [d.text for d in documents],
        }
        if top_k is not None:
            body["top_n"] = top_k
        return body

    def _parse_results(self, payload: dict[str, Any]) -> list[tuple[int, float]]:
        return [
            (int(r["index"]), float(r["relevance_score"]))
            for r in payload["results"]
        ]

    def _cost_estimate(self, payload: dict[str, Any]) -> Decimal | None:
        meta = payload.get("meta") or {}
        billed = meta.get("billed_units") or {}
        search_units = billed.get("search_units")
        if search_units is None:
            return self._price_per_call_usd  # one call = one search unit
        return self._price_per_call_usd * Decimal(int(search_units))


__all__ = ["CohereReranker"]
