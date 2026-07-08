"""Jina Reranker adapter (docs/25: jina-reranker-v2). Token-billed; cost from
usage when reported, else the defensive default applies."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from foundry.core import RetrievedDocument
from foundry.core.tool import EmitFn
from foundry.retrieval.rerankers._base import ClientProvider, HTTPReranker

_PRICE_PER_1M_TOKENS = Decimal("0.02")


class JinaReranker(HTTPReranker):
    def __init__(
        self,
        model: str,
        get_client: ClientProvider,
        *,
        emit: EmitFn | None = None,
        agent_name: str = "",
    ) -> None:
        super().__init__(
            "jina_rerank", model, get_client, emit=emit, agent_name=agent_name
        )

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
        usage = payload.get("usage") or {}
        tokens = usage.get("total_tokens")
        if tokens is None:
            return None
        return Decimal(int(tokens)) * _PRICE_PER_1M_TOKENS / Decimal(1_000_000)


__all__ = ["JinaReranker"]
