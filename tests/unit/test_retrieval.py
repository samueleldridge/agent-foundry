"""Retrievers, RRF fusion, rerankers (docs/25 § Test expectations)."""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx
import pytest

from foundry.core import Embedding, RetrievedDocument
from foundry.core.errors import RerankError, RetrievalError
from foundry.core.events import RetrievalEvent, WarningEvent
from foundry.retrieval import (
    BM25Index,
    CohereReranker,
    DenseRetriever,
    HybridRetriever,
    InMemoryVectorIndex,
    JinaReranker,
    LocalCrossEncoderReranker,
    RetrieverPipeline,
    SparseRetriever,
    VoyageReranker,
    rrf_merge,
)


def _doc(doc_id: str, score: float = 1.0, **metadata: Any) -> RetrievedDocument:
    return RetrievedDocument(
        id=doc_id, text=f"text of {doc_id}", score=score,
        source="unit", metadata=metadata,
    )


class _Emitted:
    def __init__(self) -> None:
        self.events: list[tuple[type, dict[str, Any]]] = []

    def __call__(self, event_cls: type, **fields: Any) -> None:
        self.events.append((event_cls, fields))

    def of(self, cls: type) -> list[dict[str, Any]]:
        return [f for c, f in self.events if c is cls]


class _StubRetriever:
    kind = "dense"

    def __init__(
        self,
        name: str,
        docs: list[RetrievedDocument],
        *,
        fail: Exception | None = None,
        rendezvous: asyncio.Barrier | None = None,
    ) -> None:
        self.name = name
        self._docs = docs
        self._fail = fail
        self._rendezvous = rendezvous
        self.calls = 0

    async def retrieve(
        self, query: str, top_k: int = 20, filters: Any = None
    ) -> list[RetrievedDocument]:
        self.calls += 1
        if self._rendezvous is not None:
            # Deterministic concurrency probe: this branch refuses to return
            # until every party is in-flight simultaneously. Sequential
            # execution deadlocks here (bounded by the test's wait_for).
            await self._rendezvous.wait()
        if self._fail is not None:
            raise self._fail
        return self._docs[:top_k]


# --- RRF fusion (docs/25 unit expectation 1) -----------------------------------------


@pytest.mark.unit
def test_rrf_score_formula_and_merged_order() -> None:
    dense = [_doc("a"), _doc("b"), _doc("c")]
    sparse = [_doc("b"), _doc("d")]
    merged = rrf_merge({"dense": dense, "sparse": sparse}, k=60)
    scores = {d.id: d.score for d in merged}
    assert scores["a"] == pytest.approx(1 / 61)
    assert scores["b"] == pytest.approx(1 / 62 + 1 / 61)  # rank 2 + rank 1
    assert scores["c"] == pytest.approx(1 / 63)
    assert scores["d"] == pytest.approx(1 / 62)
    assert [d.id for d in merged] == ["b", "a", "d", "c"]


@pytest.mark.unit
def test_rrf_dedupes_and_keeps_first_branch_metadata() -> None:
    dense = [_doc("a", origin="dense")]
    sparse = [_doc("a", origin="sparse")]
    merged = rrf_merge({"dense": dense, "sparse": sparse})
    assert len(merged) == 1
    assert merged[0].metadata == {"origin": "dense"}


# --- hybrid (parallel + degrade) ------------------------------------------------------


@pytest.mark.unit
async def test_hybrid_runs_branches_in_parallel_and_merges() -> None:
    # Deterministic concurrency probe (no wall-clock): each branch blocks on
    # a 2-party barrier, so the call can only complete if BOTH branches are
    # in-flight at the same time. A sequential regression deadlocks the first
    # branch and the wait_for below fails the test after its bound.
    rendezvous = asyncio.Barrier(2)
    dense = _StubRetriever("d", [_doc("a"), _doc("b")], rendezvous=rendezvous)
    sparse = _StubRetriever("s", [_doc("b"), _doc("c")], rendezvous=rendezvous)
    emitted = _Emitted()
    hybrid = HybridRetriever("kb", dense, sparse, emit=emitted, agent_name="t")

    out = await asyncio.wait_for(hybrid.retrieve("q", top_k=2), timeout=5)
    assert dense.calls == sparse.calls == 1
    assert [d.id for d in out] == ["b", "a"]  # RRF order, truncated to top_k

    event = emitted.of(RetrievalEvent)[-1]
    assert event["kind"] == "hybrid"
    assert set(event["branch_latency_ms"]) == {"dense", "sparse"}
    assert event["branches_failed"] == []


@pytest.mark.unit
async def test_hybrid_one_branch_fails_other_returns_with_warning() -> None:
    dense = _StubRetriever("d", [], fail=RetrievalError("store down"))
    sparse = _StubRetriever("s", [_doc("c"), _doc("d")])
    emitted = _Emitted()
    hybrid = HybridRetriever("kb", dense, sparse, emit=emitted, agent_name="t")
    out = await hybrid.retrieve("q", top_k=5)
    assert [d.id for d in out] == ["c", "d"]
    warning = emitted.of(WarningEvent)[0]
    assert warning["category"] == "retrieval.branch_failed"
    assert "'dense'" in warning["message"]
    event = emitted.of(RetrievalEvent)[-1]
    assert event["branches_failed"] == ["dense"]


@pytest.mark.unit
async def test_hybrid_both_branches_fail_raises_retrieval_error() -> None:
    hybrid = HybridRetriever(
        "kb",
        _StubRetriever("d", [], fail=RetrievalError("down")),
        _StubRetriever("s", [], fail=RetrievalError("also down")),
    )
    with pytest.raises(RetrievalError) as excinfo:
        await hybrid.retrieve("q")
    assert excinfo.value.context["branches_failed"] == ["dense", "sparse"]


# --- dense + sparse building blocks ---------------------------------------------------


class _FixedEmbedder:
    name = "fake:embed"
    model = "embed"
    capabilities = None

    async def embed(self, inputs: list[str], purpose: str = "document") -> list[Embedding]:
        vectors = {"q": [1.0, 0.0], "close": [0.9, 0.1], "far": [0.0, 1.0]}
        return [
            Embedding(vector=vectors.get(text, [0.5, 0.5]), dimensions=2,
                      model="embed", input_tokens=1, latency_ms=1)
            for text in inputs
        ]


@pytest.mark.unit
async def test_dense_retriever_orders_by_cosine_and_respects_filters() -> None:
    index = InMemoryVectorIndex()
    index.add("close", "close", [0.9, 0.1], metadata={"lang": "en"})
    index.add("far", "far", [0.0, 1.0], metadata={"lang": "fr"})
    retriever = DenseRetriever("d", _FixedEmbedder(), index.search)
    out = await retriever.retrieve("q", top_k=2)
    assert [d.id for d in out] == ["close", "far"]
    filtered = await retriever.retrieve("q", top_k=2, filters={"lang": "fr"})
    assert [d.id for d in filtered] == ["far"]


@pytest.mark.unit
async def test_bm25_ranks_exact_id_matches_first() -> None:
    index = BM25Index()
    index.add("FR-001", "Paris is the capital of France. Id FR-001.")
    index.add("JP-001", "Tokyo is the capital of Japan. Id JP-001.")
    retriever = SparseRetriever("s", index.search)
    out = await retriever.retrieve("FR-001", top_k=2)
    assert out and out[0].id == "FR-001"
    # semantic paraphrase without shared terms → no match (dense's job)
    assert await retriever.retrieve("municipality", top_k=2) == []


# --- rerankers ------------------------------------------------------------------------


def _rerank_client(payload: dict[str, Any]) -> Any:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json=payload)

    client = httpx.AsyncClient(
        base_url="https://api.example-rerank.test",
        transport=httpx.MockTransport(handler),
    )

    async def get_client() -> httpx.AsyncClient:
        return client

    return get_client, captured


@pytest.mark.unit
async def test_cohere_reranker_reorders_preserves_metadata_and_costs() -> None:
    docs = [_doc("a", 0.5, date="2026"), _doc("b", 0.4), _doc("c", 0.3)]
    get_client, captured = _rerank_client({
        "results": [
            {"index": 2, "relevance_score": 0.99},
            {"index": 0, "relevance_score": 0.42},
            {"index": 1, "relevance_score": 0.10},
        ],
        "meta": {"billed_units": {"search_units": 1}},
    })
    from foundry.core.events import RerankEvent

    emitted = _Emitted()
    reranker = CohereReranker(
        "rerank-english-v3.0", get_client, emit=emitted, agent_name="t"
    )
    out = await reranker.rerank("q", docs, top_k=2)
    assert [d.id for d in out] == ["c", "a"]  # reordered + truncated
    assert out[1].metadata == {"date": "2026"}  # metadata preserved
    assert out[0].score == pytest.approx(0.99)  # score replaced
    assert captured["body"]["top_n"] == 2
    event = emitted.of(RerankEvent)[0]
    assert event["cost_estimate_usd"] == Decimal("0.002")
    assert event["before_ids"] == ["a", "b", "c"]
    assert event["after_ids"] == ["c", "a"]


@pytest.mark.unit
async def test_cohere_cost_defaults_defensively_when_meta_missing() -> None:
    get_client, _ = _rerank_client(
        {"results": [{"index": 0, "relevance_score": 0.5}]}
    )
    from foundry.core.events import RerankEvent

    emitted = _Emitted()
    reranker = CohereReranker("rerank-3", get_client, emit=emitted)
    await reranker.rerank("q", [_doc("a")])
    # never None: falls back to the per-call price (docs/03 exit gate)
    assert emitted.of(RerankEvent)[0]["cost_estimate_usd"] == Decimal("0.002")


@pytest.mark.unit
async def test_voyage_and_jina_parse_their_result_shapes() -> None:
    get_voyage, _ = _rerank_client({
        "data": [{"index": 1, "relevance_score": 0.9},
                 {"index": 0, "relevance_score": 0.2}],
        "usage": {"total_tokens": 1000},
    })
    voyage = VoyageReranker("voyage-rerank-2", get_voyage)
    out = await voyage.rerank("q", [_doc("a"), _doc("b")])
    assert [d.id for d in out] == ["b", "a"]

    get_jina, _ = _rerank_client({
        "results": [{"index": 0, "relevance_score": 0.7}],
    })
    jina = JinaReranker("jina-reranker-v2", get_jina)
    out = await jina.rerank("q", [_doc("a")])
    assert out[0].score == pytest.approx(0.7)


@pytest.mark.unit
async def test_reranker_http_error_raises_rerank_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "boom"})

    client = httpx.AsyncClient(
        base_url="https://api.example-rerank.test",
        transport=httpx.MockTransport(handler),
    )

    async def get_client() -> httpx.AsyncClient:
        return client

    reranker = CohereReranker("rerank-3", get_client)
    with pytest.raises(RerankError):
        await reranker.rerank("q", [_doc("a")])


@pytest.mark.unit
async def test_local_cross_encoder_stub_raises_structured_error() -> None:
    stub = LocalCrossEncoderReranker()
    with pytest.raises(RerankError) as excinfo:
        await stub.rerank("q", [_doc("a")])
    assert excinfo.value.context["missing_dependency"] == "sentence-transformers"


# --- pipeline (rerank fall-through) ---------------------------------------------------


class _FailingReranker:
    name = "broken"
    model = "broken"

    async def rerank(
        self, query: str, documents: list[RetrievedDocument],
        top_k: int | None = None,
    ) -> list[RetrievedDocument]:
        raise RerankError("service down")


@pytest.mark.unit
async def test_pipeline_falls_through_on_reranker_failure_with_warning() -> None:
    docs = [_doc("a"), _doc("b")]
    emitted = _Emitted()
    pipeline = RetrieverPipeline(
        "kb", _StubRetriever("d", docs), _FailingReranker(),
        default_top_k=5, reranker_top_k=1, emit=emitted, agent_name="t",
    )
    out = await pipeline.retrieve("q")
    assert [d.id for d in out] == ["a", "b"]  # unreranked docs survive
    warning = emitted.of(WarningEvent)[0]
    assert warning["category"] == "rerank.fallthrough"
