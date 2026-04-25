# 25 — Retrieval and RAG

## Purpose

This doc specifies the foundry's **retrieval** and **RAG (Retrieval-Augmented Generation)** primitives: `Retriever`, `Reranker`, `RetrievedDocument`. It covers the three retrieval strategies (dense, sparse, hybrid), cross-encoder reranking, agent integration patterns (tool-style vs auto-retrieve), catalog templates for common backends, and the RAG patterns (query rewriting, multi-query, HyDE) that remain consumer-code rather than framework primitives.

Production RAG is rarely "embed and search" on its own. Real pipelines layer:

1. Dense retrieval (vectors) for semantic matches.
2. Sparse retrieval (BM25 / lexical) for exact terms, names, IDs.
3. Hybrid fusion to combine both.
4. Cross-encoder reranking to rescore the top-K for relevance.
5. Optional: query rewriting or multi-query to broaden recall; HyDE for hard-to-express queries.

The primitives here support all of that compositionally.

## Mental model

```
   agent or tool handler
          │
          ▼
    ┌─────────────────────────────────────────────┐
    │  Retriever.retrieve(query, top_k=50)        │
    │                                             │
    │  ├── DenseRetriever                         │
    │  │     → Embedder.embed(query, query)       │
    │  │     → vector-store connection search     │
    │  │                                          │
    │  ├── SparseRetriever                        │
    │  │     → sparse-search connection (BM25)    │
    │  │                                          │
    │  └── HybridRetriever                        │
    │        → DenseRetriever in parallel with    │
    │        → SparseRetriever                    │
    │        → merge via RRF or weighted          │
    └─────────────────────────────────────────────┘
          │
          ▼    list[RetrievedDocument]  (50 candidates)
    ┌─────────────────────────────────────────────┐
    │  Reranker.rerank(query, docs, top_k=8)      │
    │    → cross-encoder service (Cohere / Voyage │
    │      / Jina / local)                        │
    │    → rescore, reorder, truncate             │
    └─────────────────────────────────────────────┘
          │
          ▼    list[RetrievedDocument]  (8 reranked)
    agent prompt / tool result
```

Every stage emits observability events. Every stage is swappable via config.

## Primitives

Defined in `10-core-framework.md` — protocols and types. Summary and behavioural contract below.

### `Retriever`

```python
class Retriever(Protocol):
    name: str
    kind: Literal["dense", "sparse", "hybrid"]

    async def retrieve(
        self,
        query: str,
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedDocument]: ...
```

**Contract**:
- Returns a list ordered best-to-worst by the retriever's own scoring.
- `score` on each `RetrievedDocument` is retriever-specific (cosine similarity for dense, BM25 for sparse, RRF rank for hybrid). Cross-retriever comparisons are only meaningful via rank-based fusion, not raw scores.
- `filters` are translated by each retriever into its backing store's filter language. Standard keys (`source`, `date_gte`, `date_lte`, `tags`) have conventions; arbitrary keys are passed through and validated by the backing store.
- Deterministic for the same query + filters (modulo backing-store eventual consistency).

### `Reranker`

```python
class Reranker(Protocol):
    name: str
    model: str

    async def rerank(
        self,
        query: str,
        documents: list[RetrievedDocument],
        top_k: int | None = None,
    ) -> list[RetrievedDocument]: ...
```

**Contract**:
- Input list order is ignored; reranker scores fresh via cross-encoder.
- Output `score` is replaced with the reranker's score (typically `0.0`–`1.0`).
- Truncated to `top_k` if provided; else all input docs returned in the new order.
- Metadata preserved from input (id, text, source, metadata).

### `RetrievedDocument`

```python
class RetrievedDocument(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    text: str
    score: float
    source: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

Immutable once retrieved. Passing through stages produces new instances with updated `score`.

## Retrieval strategies

### Dense retrieval

Semantic similarity via vector embeddings. Strong for "find me docs about X" queries where exact wording doesn't match.

**Needs**: an `Embedder` (for query + document ingestion) and a vector-store connection (pgvector / Pinecone / Qdrant / Weaviate / Milvus).

**Weakness**: misses exact-term matches (entity IDs, names, acronyms). "Show me everything on trade ID ABC123" does not reliably work through dense-only retrieval.

### Sparse retrieval

Lexical matching via BM25 or vendor sparse vectors (Elasticsearch, OpenSearch, Pinecone sparse, SPLADE-style). Strong for exact terms, names, acronyms, IDs.

**Needs**: a sparse-search connection (Elasticsearch / OpenSearch) OR a hybrid index that stores sparse vectors alongside dense.

**Weakness**: misses semantic paraphrases. "Cases involving unauthorised trades" does not match a document titled "Rogue trading incidents."

### Hybrid retrieval

Runs dense + sparse in parallel; merges results.

**Merge strategies**:

- **Reciprocal Rank Fusion (RRF)**: rank-based, parameter-free (`score = sum(1 / (k + rank_i))` across retrievers, typically `k=60`). Robust default; no score normalisation needed.
- **Weighted linear combination**: `score = w_dense * normalise(dense_score) + w_sparse * normalise(sparse_score)`. Requires per-retriever score normalisation; finicky but can be tuned.
- **Query-dependent routing**: simple classifier decides per-query which retriever to favour. Overkill for most cases.

Default for `HybridRetriever`: RRF with `k=60`.

### Ranking comparison

| Strategy | Recall@50 on general queries | Recall@50 on exact-ID queries | Latency |
|---|---|---|---|
| Dense only | Typically highest on semantic | Often miserable | ~embedding + vector search |
| Sparse only | Weaker on paraphrase | Near-perfect on exact | ~BM25 search |
| Hybrid (RRF) | Closes both gaps, ~5–15% above best single | Best of both | Sum of above, parallelised |
| Hybrid + Rerank | Best on precision@k | Best precision | + reranker latency |

For most production RAG on mixed-content corpora: **hybrid + reranker** is the default recommendation.

## Rerankers

Cross-encoder models score `(query, document)` pairs directly, using both as joint input. More accurate than embedding similarity but too expensive for initial retrieval over large corpora — hence the retrieve-then-rerank pattern.

**Supported vendors**:
- **Cohere Rerank** — `rerank-3`, `rerank-english-v3.0`, `rerank-multilingual-v3.0`. Widely used; good multilingual.
- **Voyage Rerank** — `voyage-rerank-2`. Strong quality, often paired with Voyage embeddings.
- **Jina Reranker** — `jina-reranker-v2`. Open-weight options available.
- **Local cross-encoder** — Sentence-Transformers cross-encoder served via ONNX or TorchServe. Zero external egress; operationally heavier.

Each ships as a catalog connection + retriever adapter.

### When a reranker earns its keep

- **Precision@k matters** (the agent will only read the top few docs — injecting irrelevant docs dilutes the context).
- **Initial recall is high** (retrieval returns 30–100 candidates; rerank selects the best 5–10).
- **Latency budget allows** ~100–300ms per rerank call (typical for cross-encoder services at reasonable sizes).

Rerankers do not help if retrieval's recall is already low — you can't rerank a bad candidate set into a good one.

### Cost-aware use

Rerankers bill per query. A batch of 50k retrieval calls reranking 50 docs each is 50k rerank calls. Price tiers matter. Track via `foundry.rerank` observability events; budget accordingly.

## Configuration

### `RetrieverBinding` on `AgentSpec`

Agents declare retrievers they can access:

```yaml
# projects/<name>/agents/<agent>/agent.yaml
retrievers:
  - slot: knowledge_base
    ref: catalog/hybrid_rrf
    version: v1
    connection_bindings:
      dense_store: prod_pgvector
      sparse_store: prod_elastic
    reranker:
      ref: catalog/cohere_rerank
      version: v1
      connection_bindings:
        cohere: cohere_api
      top_k: 8
    top_k: 50
```

Slots mirror the tool-slot pattern. The agent's tool-dispatch path or pre-agent retrieval step calls `ctx.retrievers.get(slot)` — parallel to `ctx.connections.get(slot)`.

### Catalog templates

Three kinds of catalog entries enable RAG:

**1. Retriever templates** (`catalog/retrievers/`):

| Template | Kind | Composes |
|---|---|---|
| `pgvector_dense` | dense | `Embedder` + pgvector connection |
| `pinecone_dense` | dense | `Embedder` + Pinecone connection |
| `qdrant_dense` | dense | `Embedder` + Qdrant connection |
| `elastic_bm25` | sparse | Elasticsearch connection |
| `opensearch_bm25` | sparse | OpenSearch connection |
| `hybrid_rrf` | hybrid | dense retriever slot + sparse retriever slot, RRF fusion |
| `hybrid_weighted` | hybrid | dense + sparse, weighted linear fusion with tunable weights |

**2. Connection templates** (`catalog/connections/`) — new additions:

| Connection | Auth | Notes |
|---|---|---|
| `pgvector` | same as `postgres` — basic / IAM / mTLS | pgvector extension assumed installed |
| `pinecone` | api_key | managed; no self-hosting |
| `qdrant` | api_key / mTLS | self-host or cloud |
| `elasticsearch` | basic / api_key / mTLS | Elastic Cloud or self-host |
| `opensearch` | basic / SigV4 (AWS) | AWS-managed or self-host |
| `cohere_api` | api_key | rerank + embeddings |
| `voyage_api` | api_key | rerank + embeddings |

**3. Embedder templates** implicit via the `EmbedderBinding` in semantic cache or dense retriever configs. No separate catalog category.

### Agent integration patterns

Two distinct patterns, both supported:

#### Pattern A: Retrieval as a tool

The LLM decides when to retrieve. Agent declares a tool (e.g. `search_knowledge_base`) whose handler uses `ctx.retrievers.get(...)`. Standard tool-use flow.

Pros: LLM chooses query wording and when to retrieve; handles multi-step research flows naturally.
Cons: each retrieve is a separate LLM round-trip; can miss retrieval opportunities if prompt doesn't cue it.

Example tool:

```python
# in projects/<name>/tools/search_knowledge_base/v1/handler.py
async def handle(inputs: SearchIn, ctx: RunContext) -> SearchOut:
    retriever = ctx.retrievers.get("knowledge_base")
    docs = await retriever.retrieve(inputs.query, top_k=20, filters=inputs.filters)
    return SearchOut(documents=[d.model_dump() for d in docs])
```

#### Pattern B: Auto-retrieve before agent starts

A pre-agent step runs retrieval deterministically (no LLM involved), injects results into state, and the agent sees them in its initial prompt. Good for agents whose job is "synthesise an answer from retrieved context."

Implemented as an orchestration pattern: a `retrieve` node precedes the agent node in the flow; its output populates a state field the agent reads.

Example flow config:

```yaml
flow:
  type: sequential
  steps:
    - retrieve_context        # a retrieval-only node, no LLM
    - synthesise_answer       # agent that reads state.retrieved_docs
```

Pros: deterministic, one fewer round-trip, always retrieves.
Cons: doesn't adapt to multi-step research; can waste LLM tokens on irrelevant retrievals.

Both patterns often co-exist in one project. A common shape: auto-retrieve provides baseline context; a `follow_up_search` tool lets the LLM do targeted lookups when baseline isn't enough.

## RAG patterns (consumer code, not primitives)

These are documented here so implementations stay consistent; they are implementable today without framework changes.

### Query rewriting

Before retrieval, use a cheap LLM call to rewrite the user's query into one better-suited for retrieval (expand abbreviations, add synonyms, clarify intent).

Implementable as a tiny agent node (`rewrite_query`) that runs before the retrieve step in Pattern B, or as a tool the main agent calls in Pattern A.

### Multi-query retrieval

Generate N query variants, retrieve for each, merge results (dedupe by doc id, rank by max or mean score). Improves recall at the cost of N× retrieval spend.

Implementable as a node that produces `list[str]`, followed by a fan-out retrieve (`create_task_group` over each variant), followed by merge.

### HyDE (Hypothetical Document Embeddings)

For hard-to-express queries, ask the LLM to generate a hypothetical document that would answer the query, then embed *that* for retrieval. Often improves recall on abstract queries. Implementable as a two-step agent: `generate_hypothesis` → `retrieve(using hypothesis as query)`.

### Citation-grounded generation

After retrieval, the agent's prompt template includes `<doc id="...">text</doc>` markers and the agent is instructed to cite sources by id in its output. The output schema validates that cited ids correspond to retrieved docs (a Pydantic validator). Not a framework feature — a prompt + schema pattern.

### Relevance scoring as output

The agent rates each retrieved doc's relevance in its output. The eval harness compares the agent's ranking against human-labelled relevance judgments. Not a framework feature but a useful eval pattern documented in `40-eval-harness.md` (when written).

## Observability

Every retrieval and rerank emits events (`foundry.retrieval`, `foundry.rerank`) with attributes defined in `10-core-framework.md` and `01-architecture-overview.md`.

Derived metrics (surface in `foundry obs`):

- `foundry.retrieval.latency_ms` — by retriever kind.
- `foundry.retrieval.returned` — distribution of result-set sizes.
- `foundry.rerank.cost_usd` — by reranker.
- `foundry.rerank.latency_ms` — by reranker model.

Debug question answered by a single event-stream query: "for run X, what did the retriever return, what did the reranker keep, and what did the LLM see in its prompt?"

## Failure modes

| Cause | Surfaced as |
|---|---|
| Vector store unreachable | `ConnectionError` (the retrieval connection's standard error) — retrieval aborts, agent sees empty doc list, can fall back to tool-free reasoning or error |
| Embedder unavailable | `EmbedderError` — dense retrieval fails; hybrid retriever degrades to sparse-only with a warning event |
| Reranker timeout | falls through with unreranked docs + warning event (configurable to hard-fail) |
| Filter schema mismatch at backing store | `CompileError` at binding validation (where possible) or `RetrievalError` at call time |
| Index schema drift (doc count zero, dimensions wrong) | startup health-check of the retrieval connection flags it before production traffic |

**Fail-degradation rule**: in hybrid retrieval, a failure in one branch falls through to the other with a warning. The agent still gets documents, just from a narrower retriever. Full failure → empty doc list + metric alert.

## Invariants

1. **Retrievers are stateless across calls.** All state lives in the backing store. Restarting a worker does not lose retrieval capability.
2. **Rerankers do not introduce documents.** They can only reorder and truncate the input list — never fetch new docs.
3. **`RetrievedDocument` is immutable.** Stage-to-stage transformations produce new instances with updated `score`.
4. **Retrieval observability is complete.** Every retrieve and rerank call emits an event; no silent retrievals in the audit trail.
5. **Filter keys are validated where possible.** Standard keys have types; unknown keys pass through but are logged at info level for visibility.
6. **Dimension agreement enforced at compile.** Dense retrievers validate the embedder's dimensions match the vector store's configured dimensions.

## Test expectations

### Unit

1. **RRF fusion correctness**: given two ranked lists, assert RRF score formula holds and merged order is correct.
2. **Reranker preserves metadata**: input doc with `metadata={"date": ...}` appears in output with same metadata, different `score`.
3. **Filter pass-through**: unknown filter key reaches the backing store's filter translator; standard key is validated against type.
4. **Dimension mismatch check**: compile fails if embedder dims ≠ vector store dims.
5. **Hybrid degrade-on-failure**: sparse branch raises; dense result returned with a warning event.

### Integration (Phase 2 exit gate extension)

1. End-to-end: agent declares `hybrid_rrf` retriever bound to a pgvector + Elasticsearch test fixtures; retrieval returns docs; rerank narrows to top_k; agent response cites retrieved docs.
2. Connection health check for a retrieval connection validates the index has documents and advertises correct dimensions.
3. A failed reranker call (network error) produces a `run.completed` with unranked docs + warning event, not a run failure.

## Catalog template details

Detailed schemas and factory implementations are catalog-side. Each retriever template includes:

- `retriever.yaml` — `RetrieverSpec` (name, version, kind, description, slots required).
- `factory.py` — async factory that builds the concrete `Retriever` given configs + connection handles.
- `schemas.py` — config schema.
- `health.yaml` — health-check eval (one trivial query that must return ≥1 doc from a known-good test collection).
- `README.md`.

Same shape as tool and connection catalog entries. Discoverable via `foundry catalog list retrievers`.

## Open questions

1. **Corpus ingestion workflow.** RESOLVED 2026-04-25: NO ingestion primitive in v1. Document the pattern (chunk → embed → upsert) per backing store; promote to a primitive only if 3+ projects ask. Ingestion is bespoke per corpus.
2. **Retrieval caching.** RESOLVED 2026-04-25: DEFERRED. Add metrics first; revisit if hit-rate would justify for batch workloads. Not in v1.
3. **Vector store schema migrations.** Re-embedding 100M documents when swapping embedders is an operator's problem, but the foundry should surface guidance. Add a `foundry retrieval migrate` stub in Phase 9 for documentation; actual migration logic is backend-specific.
4. **Relevance feedback loops.** Agents mark retrieved docs as "useful" or not; the signal feeds back into the retriever (learned reranker, click-through biasing). Out of scope for v1; noted for research.
5. **Hybrid weights in production.** The weighted-linear merge strategy has tunable weights. Is this a per-deployment setting that the meta-agent can iterate on via evals? Lean: yes — treat weights as a config parameter the meta-agent's `compare_versions` workflow can vary. Small extension.
