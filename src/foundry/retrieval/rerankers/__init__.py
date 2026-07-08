"""Cross-encoder reranker adapters: Cohere, Voyage, Jina (+ a local
cross-encoder stub). See docs/25 § Rerankers."""

from __future__ import annotations

from foundry.retrieval.rerankers._base import ClientProvider, HTTPReranker
from foundry.retrieval.rerankers.cohere import CohereReranker
from foundry.retrieval.rerankers.jina import JinaReranker
from foundry.retrieval.rerankers.local import LocalCrossEncoderReranker
from foundry.retrieval.rerankers.voyage import VoyageReranker

__all__ = [
    "ClientProvider",
    "CohereReranker",
    "HTTPReranker",
    "JinaReranker",
    "LocalCrossEncoderReranker",
    "VoyageReranker",
]
