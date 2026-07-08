"""Concrete retrieval layer (docs/25): dense / sparse / hybrid retrievers,
reranker adapters, in-memory indexes for no-infra projects, and the
compile-time binding wiring + run-time pipeline construction."""

from __future__ import annotations

from foundry.retrieval.dense import DenseRetriever, InMemoryVectorIndex, VectorSearchFn
from foundry.retrieval.hybrid import DEFAULT_RRF_K, HybridRetriever, rrf_merge
from foundry.retrieval.rerankers import (
    ClientProvider,
    CohereReranker,
    HTTPReranker,
    JinaReranker,
    LocalCrossEncoderReranker,
    VoyageReranker,
)
from foundry.retrieval.sparse import BM25Index, SparseRetriever, SparseSearchFn
from foundry.retrieval.wiring import (
    MappingRetrieverAccessor,
    PreparedReranker,
    PreparedRetriever,
    RetrieverBuildContext,
    RetrieverPipeline,
    build_retriever_accessor,
    prepare_reranker,
    prepare_retriever,
    prepare_retrievers,
)

__all__ = [
    "DEFAULT_RRF_K",
    "BM25Index",
    "ClientProvider",
    "CohereReranker",
    "DenseRetriever",
    "HTTPReranker",
    "HybridRetriever",
    "InMemoryVectorIndex",
    "JinaReranker",
    "LocalCrossEncoderReranker",
    "MappingRetrieverAccessor",
    "PreparedReranker",
    "PreparedRetriever",
    "RetrieverBuildContext",
    "RetrieverPipeline",
    "SparseRetriever",
    "SparseSearchFn",
    "VectorSearchFn",
    "VoyageReranker",
    "build_retriever_accessor",
    "prepare_reranker",
    "prepare_retriever",
    "prepare_retrievers",
    "rrf_merge",
]
