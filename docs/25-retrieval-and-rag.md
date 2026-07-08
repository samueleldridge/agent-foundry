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

## Few-shot learning examples (FSLs)

Many production agents benefit from few-shot examples in their prompts — concrete (input, expected_output) pairs that demonstrate the desired behaviour. The foundry supports three FSL patterns from increasing-sophistication, all built on existing primitives.

### Pattern A: static FSLs in the prompt (always supported, simplest)

Hand-curated examples written directly into the agent's prompt file:

```markdown
<!-- prompts/v3.md -->
# Task
Investigate the break and recommend an action.

# Examples

Input: {trade_id: "ABC123", mismatch_usd: 12500, ...}
Output: {root_cause: "late_amendment", recommended_action: "auto_resolve", confidence: 0.92, ...}

Input: {trade_id: "XYZ789", mismatch_usd: 87000, ...}
Output: {root_cause: "partial_settlement", recommended_action: "escalate", confidence: 0.78, ...}

# Now investigate the actual case below.
```

Versioned with the prompt; trivially supported; zero new primitives.

**When to use**: small set of stable, representative examples that don't depend on the input. Good for setting tone, output shape, and edge-case handling.

**Limits**: prompt size grows with example count; static set can't adapt to varied input distributions.

### Pattern B: dynamic FSL retrieval (RAG over examples)

The agent has a `Retriever` bound to an "FSL corpus" — a vector store of historical (input, expected_output) pairs. At runtime, the agent retrieves the top-K most similar examples to the current input and injects them into the prompt.

This is RAG with the corpus being examples instead of documents. Same primitives (`Retriever`, `Reranker`, `Embedder`); same configuration shape.

```yaml
# agent.yaml
retrievers:
  - slot: fsl_lookup
    ref: catalog/dense_retriever
    version: v1
    connection_bindings:
      vector_store: fsl_pgvector
    top_k: 5
    reranker:
      ref: catalog/cohere_rerank
      version: v1
      top_k: 3
```

In the agent's prompt template:

```markdown
{{MEMORY_PREFIX}}

# Examples relevant to this case
{{retriever:fsl_lookup}}     ← framework injects top-3 reranked FSLs

# Current case
{{user_input}}
```

The corpus is populated by:
- Hand-curating high-quality examples into a YAML file → batch-ingest into the vector store.
- Capturing production runs that operators marked as exemplary (analogous to `foundry eval capture` for eval cases — see Pattern C).
- Lifting top-scoring eval cases (Pattern C below).

**When to use**: large pool of examples where input-similarity matters (different break types, different domains, different complexity levels). Adapts to the input distribution.

**Limits**: requires the corpus to be populated and maintained; cold-start has no examples.

### Pattern C: adaptive FSLs from eval results

Eval cases that scored well are high-quality FSL candidates by construction — they're explicitly labelled (input, expected_output) pairs that the agent did handle correctly. Pipeline:

1. After every eval run, the harness can write passing cases into an "FSL corpus" (a vector store catalog connection — `pgvector_dense` or similar).
2. The agent's retriever (Pattern B) is bound to that corpus.
3. At runtime, the agent retrieves examples from its own track record.

This is fully composable from existing v1 primitives — no new abstractions needed. Pieces:

- `EvalRunResult.per_case` (per `40-eval-harness.md`) provides the raw cases with scores.
- `Embedder` + vector store connection (per `25` § Retrieval strategies) provides the corpus.
- A function node or background script ingests passing cases into the corpus.
- A retriever binds the agent to the corpus.

The orchestration is consumer code; the foundry primitives support it.

**When to use**: mature projects with substantial eval history (50+ passing cases); want the agent to learn from its own validated track record over time.

**Limits**: drift risk — if the eval set is stale, the FSL corpus will be too. Mitigation: TTL on FSL entries + re-validation against current production behaviour.

### Catalog template (recommended addition)

A catalog template `catalog/retrievers/fsl_from_evals/v1/` packaging Pattern C: configures a dense retriever over a vector store + ships an ingestion function-node that reads `EvalRunResult` artifacts + populates the corpus. Operators get the wiring for free.

Spec'd as a v1 polish item; ship in Phase 5 alongside catalog promotion or Phase 9 dev-UX.

### What's deferred to v1.1+

- **Best-performing FSL for similar input** as a built-in retriever kind that auto-curates from eval results without operator wiring.
- **Per-FSL effectiveness tracking** — which examples actually helped on which inputs, surfaced in observability for tuning.
- **Adaptive FSL TTL + re-validation** — automatically retire stale examples that no longer match current production patterns.

These compound the value of Pattern C but require investment beyond v1 scope.

## Vendor-managed RAG services

Cloud vendors increasingly offer end-to-end managed RAG services that hide the embedder + vector store + reranker behind a single API. Examples as of 2026:

| Vendor | Service | Shape |
|---|---|---|
| Google Cloud | Vertex AI Search (Discovery Engine) | Point at a GCS bucket; auto-ingests + indexes; query API returns ranked documents |
| AWS | Bedrock Knowledge Bases | Point at S3; choose embedder + vector store (managed OpenSearch / Pinecone / pgvector); query via Retrieve / RetrieveAndGenerate API |
| Azure | Azure AI Search (with vectorization) | Index Azure Blob / Cosmos DB / SQL content; vector + lexical hybrid first-class |
| Anthropic / OpenAI | Built-in file-search tools (where available) | Provider-side document handling; provider-specific |

These fit cleanly into the foundry as **`Retriever` implementations with `kind: "managed"`** — they return `list[RetrievedDocument]` like any other retriever; the embedder + vector store + reranker are vendor-internal and not configurable separately.

### Wiring as a foundry retriever

The agent's view doesn't change between DIY and managed:

```yaml
# DIY hybrid retrieval:
retrievers:
  - slot: ops_manuals
    ref: catalog/hybrid_rrf
    version: v1
    connection_bindings:
      dense_store: prod_pgvector
      sparse_store: prod_elastic
    reranker:
      ref: catalog/cohere_rerank
      version: v1
      connection_bindings: {cohere: cohere_api}
    top_k: 10

# vs managed (same agent code; different retriever ref):
retrievers:
  - slot: ops_manuals
    ref: catalog/vertex_ai_search
    version: v1
    connection_bindings:
      gcs_bucket: prod_ops_docs
      vertex_endpoint: vertex_search_prod
    top_k: 10
```

The handler.py for the managed retriever is a thin wrapper around the vendor's API — typically 100–200 lines that translates `Retriever.retrieve(query, top_k, filters)` into the vendor's call shape. Catalog templates ship per vendor.

### When to choose managed vs DIY

| Aspect | Managed (Vertex AI Search etc.) | DIY (pgvector + Cohere etc.) |
|---|---|---|
| Setup time | Minutes (point at bucket) | Days (provision DB, embed corpus, wire) |
| Maintenance burden | Vendor handles | You handle (DB upkeep, re-embedding on model swap, scaling) |
| Auto-ingestion (bucket changes → indexed) | ✅ Built-in | Build your own pipeline |
| Embedder + ranker choice | Vendor-defaulted; limited tuning | Full control |
| Cost model | Per-query + per-document; vendor-set | Per-resource (DB, embedder API); more knobs |
| Hybrid (dense + sparse) | Often built-in; opaque tuning | Configure RRF / weights yourself |
| Eval-driven retriever swap | Awkward (different vendor APIs to switch between) | Trivial (swap retriever ref) |
| Vendor lock-in | Real; mitigated by foundry's `Retriever` abstraction | Minimal |
| Native security (IAM, encryption, audit) | ✅ Inherits cloud-platform controls | Configure yourself |
| Best-quality retrieval | Variable; vendor evolves | You can tune to your corpus |

### Recommended posture

For most projects, **start managed; migrate to DIY if you outgrow it**. The foundry's `Retriever` abstraction makes the migration trivial: swap one config block + one catalog ref. The only things that change are the retriever's underlying mechanics; the agent doesn't notice.

**Stay managed when**:
- Corpus is moderately stable + the vendor's defaults work well.
- You already use the vendor's cloud (IAM / encryption / audit are aligned).
- Operational burden of self-hosting a vector store isn't worth the control gain.
- Cost is acceptable + predictable enough for your scale.

**Move to DIY when**:
- You need eval-driven embedder selection (managed services hide this).
- You need a specific reranker (Cohere / Voyage / local cross-encoder) the vendor doesn't expose.
- You're hitting per-query cost ceilings + want to amortise across self-hosted infra.
- The vendor's hybrid-fusion strategy isn't right for your corpus.
- You want to use the same retriever across multiple cloud providers (vendor lock-in matters).

The `Retriever` protocol is the seam. Stay vendor-agnostic at the agent level; pick implementations per project + per environment.

### CMEK + data residency (cloud-vendor concern, not foundry concern)

Customer-managed encryption keys, region constraints, sovereign-cloud isolation are configured at the **cloud platform level** (GCP CMEK on the GCS bucket; AWS KMS on S3; Azure customer-managed keys). The foundry inherits whatever the bucket / index has — its job is to use the configured resources via standard SDK auth (workload identity, IAM roles, managed identity).

For regulated workloads with strict data sovereignty (financial services, healthcare, public sector), the configuration shape is consistent regardless of vendor:
- Bucket / index in the approved region (e.g. `europe-west2` for UK-resident data).
- LLM provider in the same region (Vertex Gemini in `europe-west2` OR Anthropic via Bedrock in `eu-west-1`).
- Foundry deployed in the same region (Cloud Run / GKE / equivalent in `europe-west2`).
- Audit + observability backends in-region.

The foundry's primitives compose without forcing cross-region traffic; region selection is the operator's call. Capability-required checks (per `11-provider-abstraction.md`) catch attempts to use unavailable features in a constrained region at compile time.

### Catalog templates (queued for v1.1)

Vendor-managed retriever + connection templates not yet shipped in the foundry's `catalog/public/`. Tracked in v1.1 backlog memory. Initial set:

- `catalog/retrievers/vertex_ai_search/v1/`
- `catalog/retrievers/bedrock_knowledge_base/v1/`
- `catalog/retrievers/azure_ai_search/v1/`
- `catalog/connections/gcs_bucket/v1/`
- `catalog/connections/vertex_endpoint/v1/`
- `catalog/connections/bigquery/v1/`
- `catalog/connections/aws_s3/v1/`
- `catalog/connections/azure_blob/v1/`

Each is a thin wrapper (~100–200 lines per template); no framework changes needed. Operators can build these locally today as `local/` artifacts following the catalog tool / connection / retriever shapes.

## Schema introspection for tools (a related pattern)

When an agent calls a tool that targets a structured data system (Snowflake, Postgres, REST API with OpenAPI), the agent often needs to know the data schema to construct the right call. Three patterns:

### A. Schema in the tool's README + description (default for stable schemas)

The tool's `README.md` and `tool.yaml.description` document the schemas the tool operates against. The framework injects tool descriptions into the agent's prompt (via `{{TOOL_SUMMARIES}}`); the agent reads the documented schema and constructs calls accordingly.

✅ Trivial to set up.
⚠️ Goes stale; manual sync when the underlying schema changes.

### B. `describe_schema(target)` tool (dynamic, recommended for evolving schemas)

A separate tool the agent calls before constructing data-system calls. Returns the current schema for a table / endpoint:

```yaml
# catalog/tools/describe_schema/v1/tool.yaml (recommended catalog template)
name: describe_schema
description: |
  Return the schema for a given target (table, endpoint, etc.) of the
  bound data system. Use this BEFORE constructing queries when you're
  unsure of the schema.

input_schema: schemas.py::DescribeIn
output_schema: schemas.py::SchemaDescription

# Schema introspection is idempotent within the TTL window — same target
# returns the same schema until the underlying system changes. Mark it
# cacheable so repeated calls (same run, same project, multiple agents)
# don't re-hit the data system. See 24-caching-and-optimisation.md § Tool-
# result caching.
cacheable: true
cache_ttl_s: 1800              # 30 minutes; balance freshness vs savings
cache_scope: project           # shared across agents in this project

connections_required:
  - slot: data_system
    accepts: [catalog/snowflake, catalog/postgres, catalog/openapi_service]
```

The handler is connection-kind-aware: for Snowflake it queries `INFORMATION_SCHEMA`, for OpenAPI services it parses the OpenAPI doc, etc. Catalog ships per-connection-kind variants.

✅ Always current; agent self-discovers.
✅ Tool-result cache eliminates re-introspection within the TTL window.
⚠️ Adds round-trips for first-call-of-window; subsequent calls are cache hits.

### TTL tuning for schema caching

| TTL | When | Tradeoff |
|---|---|---|
| 5–10 min | DBA pushes schema changes hourly; ops cares about catching them within minutes | More cache misses; more lookups per day |
| 30 min (default) | Typical production schema stability window | Balanced |
| 1–4 hr | Very stable schemas; cost-sensitive workloads | Risk if DBA pushes a rename mid-day; manual `foundry cache evict --tool describe_schema --project <name>` to refresh |

Operators tune per environment. Default of 30 min works for most production schemas.

### Connection-level schema caching (deferred)

A natural enhancement: the connection's `client_type` itself carries a `.schema(target)` method that caches across the pool — no separate `describe_schema` tool dispatch needed. The agent reads schema directly from the connection client.

Benefits over tool-result caching:
- No tool-dispatch overhead.
- Pool-wide cache shared across all runs using the same connection (slightly broader than `cache_scope: project`).

Why deferred to v1.1+:
- Tool-result caching covers ~95% of the value in v1.
- Adds complexity to the connection abstraction.
- Not all connection kinds need it (only structured-data ones).
- Operators uncomfortable with the extra implicit caching can skip the v1.1 enhancement and stay on the explicit `describe_schema` tool.

For v1: ship `describe_schema` with `cacheable: true` per the recommended config above. Connection-level introspection is a v1.1+ enhancement when real ops demand it.

### C. Schema baked into the connection (deferred)

Connection's `client_type` could expose a `.schema()` method per connection kind. Agent reads `ctx.connections.get(slot).schema(table)` directly without a separate tool. More integrated; doesn't require an extra tool dispatch.

Deferred to v1.1+. The connection protocol in `23-connections-and-auth.md` doesn't currently mandate a schema-introspection method; adding it cleanly across connection kinds is non-trivial.

For v1: ship `describe_schema` as a recommended catalog tool for each connection kind that benefits (Snowflake, Postgres, OpenAPI services). Document in tool READMEs that agents calling structured-data tools should use `describe_schema` before constructing queries when in doubt.

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

**Reranker artifacts resolve under the `retriever` artifact kind.** A reranker
template lives under `<root>/retrievers/<name>/v<N>/`, shares the same 5-file
shape, and declares `kind: reranker` in its `retriever.yaml`. There is no
separate `reranker` `ArtifactRef` kind — `RerankerBinding.ref` resolves through
the retriever resolution path. The `kind` field is enforced in **both
directions** at compile time: binding a non-`reranker` artifact under a
`reranker:` block is a `CompileError`, and binding a `kind: reranker` artifact
as the retriever itself is equally rejected.

## Open questions

1. **Corpus ingestion workflow.** RESOLVED 2026-04-25: NO ingestion primitive in v1. Document the pattern (chunk → embed → upsert) per backing store; promote to a primitive only if 3+ projects ask. Ingestion is bespoke per corpus.
2. **Retrieval caching.** RESOLVED 2026-04-25: DEFERRED. Add metrics first; revisit if hit-rate would justify for batch workloads. Not in v1.
3. **Vector store schema migrations.** Re-embedding 100M documents when swapping embedders is an operator's problem, but the foundry should surface guidance. Add a `foundry retrieval migrate` stub in Phase 9 for documentation; actual migration logic is backend-specific.
4. **Relevance feedback loops.** Agents mark retrieved docs as "useful" or not; the signal feeds back into the retriever (learned reranker, click-through biasing). Out of scope for v1; noted for research.
5. **Hybrid weights in production.** The weighted-linear merge strategy has tunable weights. Is this a per-deployment setting that the meta-agent can iterate on via evals? Lean: yes — treat weights as a config parameter the meta-agent's `compare_versions` workflow can vary. Small extension.
