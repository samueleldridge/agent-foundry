"""Local cross-encoder reranker — STUB (documented deviation, docs/25:
'Sentence-Transformers cross-encoder served via ONNX or TorchServe. Zero
external egress; operationally heavier.').

sentence-transformers/torch are far outside the pinned dependency set, so
this registers the shape and fails loudly + actionably at call time. It
exists so projects can pin the binding today and swap in a real serving
endpoint without config churn later.
"""

from __future__ import annotations

from foundry.core import RetrievedDocument
from foundry.core.errors import RerankError


class LocalCrossEncoderReranker:
    name = "local_cross_encoder"

    def __init__(self, model: str = "cross-encoder/ms-marco-MiniLM-L-6-v2") -> None:
        self.model = model

    async def rerank(
        self,
        query: str,
        documents: list[RetrievedDocument],
        top_k: int | None = None,
    ) -> list[RetrievedDocument]:
        raise RerankError(
            "the local cross-encoder reranker is a stub in Phase 2b: it needs "
            "'sentence-transformers' (not a pinned dependency). Use "
            "cohere/voyage/jina rerankers, or serve a cross-encoder behind an "
            "HTTP endpoint and wrap it with an HTTPReranker subclass.",
            context={
                "reranker": self.name,
                "model": self.model,
                "missing_dependency": "sentence-transformers",
            },
        )


__all__ = ["LocalCrossEncoderReranker"]
