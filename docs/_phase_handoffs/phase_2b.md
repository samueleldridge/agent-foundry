# Phase 2b handoff — cache + embedders + retrieval

**Session date:** 2026-07-07
**Branch:** `main`
**Status:** Phase 2b implementation complete; awaiting AI review + operator
manual smoke test. No live API keys exist in the dev sandbox — every
live-path assertion below was verified against `httpx.MockTransport` fakes
serving api.anthropic.com, api.voyageai.com, and api.cohere.com with only
the HTTP layer substituted (the established Phase 1/2a pattern).

## Pre-work landed first

1. `fix(core)`: the failure-path `tool.completed` event now reports the real
   `retry_count` — a shared `_RetryTracker` is updated before every attempt,
   so a raise after N retries no longer reports 0. Regression test pins
   `retry_count=2` on a 3-attempt exhaustion.
2. `docs(errata)`: docs/12 `EvalSpec.scope` now includes `"connection"`;
   docs/20 states `eval.yaml` is REQUIRED by the loader (implemented 5-file
   behaviour) and marks the handler-scaffold forbidden-import lint as
   deferred to the Phase 6 meta-tools.

## What this session built

1. **`foundry.core`** — `CacheBundle` on `Session.cache` (both layers None
   when unconfigured); `CachedToolResult` envelope + `scoped_input_hash`;
   `SemanticCacheKey.bucket()`; `RetrieverAccessor` protocol +
   `RunContext.retrievers`; new events (`cache.semantic.invalidate`,
   `cache.tool.miss/store`, `warning`; `RetrievalEvent` gains
   `branch_latency_ms`/`branches_failed`, `RerankEvent` gains
   `before_ids`/`after_ids`); errors `RetrievalError` + `RerankError`;
   `ToolRegistry.dispatch` gained the docs/24 Layer-3 steps at the seam the
   2a handoff named (lookup after input validation and BEFORE
   `tool.started`, store after output validation; every cache error fails
   open with a `warning` event).
2. **`foundry.providers.embedders`** — `EmbedderBinding`/`EmbedderSettings`;
   `EmbedderAdapter` base (batching + concurrent batch fan-out, transient
   retries, per-vector dimension verification against the advertised
   capabilities, cost from input tokens); Voyage + OpenAI + Cohere adapters
   over direct httpx; Bedrock is a REGISTERED STUB (see deviations);
   registry with credentials-free `embedder_capabilities()` so compile-time
   dimension checks run without secrets.
3. **`foundry.config.schemas`** — `ToolSpec` += `cacheable`/`cache_ttl_s`/
   `cache_scope` with the paired model validator; `AgentSpec` +=
   `semantic_cache` + `retrievers` (slot uniqueness enforced); new
   `SemanticCacheConfig`, `RetrieverBinding`, `RerankerBinding`,
   `RetrieverSpec`; `EmbedderBinding` re-exported from providers. NO
   `memory` field (2c).
4. **`foundry.config.refs` / `foundry.catalog`** — `ArtifactKind` extended
   to `retriever` + `agent_template` through the same resolution path;
   `CatalogIndex.retrievers`; `load_retriever_version` with the 5-file
   shape (retriever.yaml, factory.py, schemas.py, health.yaml, README.md).
5. **`foundry.cache`** — normative key construction (`stable_hash`,
   `model_binding_hash`, `tools_hash`, `messages_structural_hash`,
   `build_semantic_cache_key`, `agent_version_hash`); `InProcessSemanticCache`
   (SQLite + plain-Python cosine, TTL sweep, LRU cap, corrupted-entry
   eviction) and `InProcessResultCache` (SQLite); `RedisSemanticCache`/
   `RedisResultCache` and `PgVectorSemanticCache`/`PostgresResultCache` with
   lazy imports → structured `CacheBackendError` naming the missing package;
   `foundry.cache.runtime` owns the compile-time preparation (embedder
   resolution, LOAD-time dimension check → `EmbedderConfigError`,
   agent-version content hash) and the run-time lookup/store flow with
   version-marker invalidation — every failure fails OPEN.
6. **`foundry.retrieval`** — `DenseRetriever` + `InMemoryVectorIndex`;
   `SparseRetriever` + dependency-free `BM25Index`; `HybridRetriever`
   (parallel branches, RRF `1/(k+rank)` k=60, one-branch degrade with
   warning, both-fail → `RetrievalError`); `HTTPReranker` base + Cohere/
   Voyage/Jina adapters + local cross-encoder stub; `wiring.py` —
   `prepare_retriever(s)` (compile-time: ref resolution, config validation,
   generic connection-slot checks shared with tools, embedder + dimension
   check, hybrid branch recursion, `kind: reranker` enforcement) and
   `build_retriever_accessor` (run start: factory-built `RetrieverPipeline`s
   with rerank fall-through semantics).
7. **`foundry.connections`** — `validate_tool_connection_wiring` refactored
   onto a generic `validate_connection_slot_wiring` (identical error text
   for tools; retrievers/rerankers reuse it).
8. **Runtime + CLI** — `compile_project` prepares retrievers + semantic
   cache (all checks load-time); `run_project` attaches the `CacheBundle`
   to the session, does version-marker invalidation, semantic lookup keyed
   by the agent's INITIAL input (a hit short-circuits the entire LLM ⇄ tool
   loop), stores the terminal response on miss, builds retriever pipelines
   at run start and exposes them via `ctx.retrievers`; `RunResult` +
   metadata.json gain `llm_call_count` (0 on a cache hit).
9. **Catalog seeds** — `catalog/retrievers/pgvector_dense@v1` (embedder +
   pgvector connection, dimension-checked), `hybrid_rrf@v1` (nested
   dense/sparse sub-bindings + RRF), `cohere_rerank@v1` (reranker STAGE
   binding the existing `catalog/cohere_rerank` CONNECTION); index.yaml
   lists them.
10. **`projects/rag_hello`** — hero demo: `rag_agent` + cacheable
    `search_docs` tool (retrieval Pattern A) + `catalog/hybrid_rrf`
    composing two project-local in-process retrievers (`docs_dense`:
    lazily-embedded in-memory index over `corpus.json`; `docs_sparse`:
    BM25) + the Cohere rerank stage + an in_process semantic cache
    (voyage-3). Runs with zero infra beyond the three vendor APIs.

## Env vars for live runs

| Variable | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | rag_agent's model binding |
| `VOYAGE_API_KEY` | semantic-cache + docs_dense embedder (voyage-3) |
| `COHERE_API_KEY` | the `cohere_api` connection behind the rerank stage |
| `FOUNDRY_CATALOG_ROOTS` / `FOUNDRY_HOME` | as in Phase 2a |

Note: the semantic cache + tool-result cache persist under
`$FOUNDRY_HOME/cache/` (`semantic.db`, `tool_results.db`) so re-runs across
processes hit. Delete those files to reset between manual tests.

## Hero commands

```bash
export ANTHROPIC_API_KEY=... VOYAGE_API_KEY=... COHERE_API_KEY=...
uv run python -m foundry run projects/rag_hello --input '{"query": "what is the capital of France?"}'
# run it again — semantic cache hit, llm_call_count 0
uv run python -m foundry run projects/rag_hello --input '{"query": "what is the capital of France?"}'
```

## Deviations from the docs (all deliberate)

1. **Bedrock embedder is a registered stub.** InvokeModel needs SigV4
   signing (boto3/botocore — not pinned; same deferral as 2a's sigv4
   default-chain). `load_embedder` + `embedder_capabilities` resolve it so
   dimension checks work; `embed()` raises a structured
   `EmbedderConfigError` naming the missing dependency and alternatives.
2. **`RetrieverBinding` gained a `config` field** absent from the docs/12
   sketch — the dense retriever's `embedder_binding`, corpus paths, table
   names etc. must live somewhere per-project; `config` validates against
   the retriever version's config schema exactly like
   `ConnectionBinding.config`.
3. **Reranker artifacts are `retriever`-kind** (live under
   `<root>/retrievers/`, share the 5-file shape, `RetrieverSpec.kind`
   gained `"reranker"`). docs/25's `RerankerBinding.ref: catalog/cohere_rerank`
   otherwise has no kind to resolve under — the docs never assigned one.
   `EvalSpec.scope` gained `"retriever"` for their health.yaml (the same
   additive move as 2a's `"connection"`).
4. **`ResultCache.lookup` returns a `CachedToolResult` envelope**
   (`output: dict` + `cached_at`), not a bare `BaseModel` — backends cannot
   know tool output schemas; the dispatcher re-validates against
   `output_schema` and treats failures as corrupted entries (miss +
   warning + overwrite). docs/10's protocol sketch left this unresolved.
5. **Tool-cache scope is folded into the key by the dispatcher**
   (`scoped_input_hash`) rather than a new protocol parameter — the
   protocol's `(tool_ref, tool_version, input_hash)` shape is preserved.
6. **Cache hits do NOT emit `tool.started`/`tool.completed`** —
   tool_calls.jsonl keeps meaning "the handler actually ran"; the hit is
   audited via `cache.tool.hit`. (docs/24 is silent; the manual checklist's
   "only one actual tool invocation in tool_calls.jsonl" decided it.)
7. **Semantic cache granularity**: the cached unit is the agent step's
   TERMINAL response keyed by the INITIAL messages (docs/24's diagram
   caches per LLM call; with tool loops that would replay a `tool_use`
   response and re-run tools for marginal savings). A hit therefore skips
   tools entirely — inherent to semantic caching and now explicit.
8. **`cache.semantic.miss` carries `top_similarity` via a backend-side
   `last_top_similarity` attribute** — the protocol's `lookup` return shape
   (`SemanticCacheHit | None`) has no channel for the best-below-threshold
   candidate. Worth a protocol touch-up in review if it grates.
9. **Redis semantic backend computes similarity client-side** over the
   bucket's members (no RediSearch dependency); the class docstring notes
   the RediSearch upgrade path is internal to the class. Both redis +
   pgvector backends are fake-tested shapes (packages not installed) with
   real structured-error tests for the missing packages.
10. **No result-cache backend config surface** — docs/12 defines none; the
    runtime always uses the in_process SQLite store under `FOUNDRY_HOME`
    when any tool is cacheable. Redis/Postgres result caches are
    implemented + unit-tested for when a config surface lands.
11. **`tool_result` cache is process-shared per FOUNDRY_HOME**, so
    `cache_ttl_s` spans runs (the exit-gate "same run" case is the narrow
    case). TTL + scope keys bound staleness exactly as docs/24 § Layer 3
    intends.
12. **Embedder default-credentials fallback**: `load_embedder(binding)`
    without a SecretsResolver uses a minimal env-var resolver (manual
    smoke-test ergonomics). Runtime paths always thread the project's real
    `SecretsProvider`.
13. **`InMemoryVectorIndex`/`BM25Index` live in `foundry.retrieval`**, not
    a catalog artifact — they're the in-process backends docs/24/25 call
    for at dev scale (FAISS deliberately not required).

## Interface notes for Phase 2c

- `Session.cache` is a `CacheBundle`; memory layers wanting the semantic
  store should NOT reuse it — memory gets its own surface per docs/26.
- `RunContext.retrievers` (a `RetrieverAccessor`) is how the episodic
  memory layer should reach its `retriever_slot` — the wiring +
  `RetrieverPipeline` are reusable as-is; `EpisodicMemoryLayerConfig.
  retriever_slot` must name a slot in `AgentSpec.retrievers` (validate at
  compile like tool allowlists).
- `prepare_retrievers`/`build_retriever_accessor` are agent-scoped; 2c's
  multi-node flows can call them per agent without changes.
- `agent_version_hash` (spec dump + prompt text) is the content hash the
  semantic cache invalidates on — memory config landing on `AgentSpec`
  will change it and correctly invalidate (a feature, but worth knowing).
- `core/memory.py`, `core/node.py`, `core/function_node.py` remain the
  Phase 1 protocol stubs — untouched this phase.
- The runtime adapter grew ~80 lines but stays a thin adapter; the real
  compiler is still Phase 3. Do not grow `compile_project` further — the
  2b logic already lives in `foundry.cache.runtime` / `foundry.retrieval.
  wiring`.

## Exit-gate confirmation

| Gate | How verified | Outcome |
|---|---|---|
| Embedder round-trip: voyage-3 + text-embedding-3-small resolve, advertised dims | `test_voyage_3_round_trip...` (1024) + `test_openai_small_round_trip...` (1536); per-vector dim verification vs manifest | ✅ (mock) / ⏳ operator |
| Semantic cache hit on re-run: `cache.semantic.hit`, similarity ≥ threshold, saved_cost populated | `test_semantic_cache_hit_on_rerun_skips_llm_and_reports_savings` (similarity 1.0, saved_cost > 0, llm_call_count 0, NO new LLM HTTP call) | ✅ (mock) / ⏳ operator |
| Semantic cache invalidation on prompt-version bump: miss + invalidate event | `test_prompt_version_bump_invalidates_semantic_cache` (version marker → `cache.semantic.invalidate` + miss + real LLM calls) | ✅ |
| Tool-result cache: second call cached; `cache.tool.hit` | `test_tool_cache_hit_on_second_identical_call_in_same_run` (1 entry in tool_calls.jsonl, 1 hit, retrieval ran once) + dispatcher unit tests | ✅ |
| `cacheable` without `cache_ttl_s` → ConfigValidationError at load | `test_cacheable_without_ttl_fails_at_load` (exit 2, no artifact) + schema unit tests both directions | ✅ |
| Cache failure fails open: backend raises → run completes via LLM + warning | `test_semantic_cache_backend_failure_fails_open` (sqlite path = directory) + `test_cache_failure_fails_open_with_warning_events` (dispatcher) | ✅ |
| Hybrid retriever: parallel dense+sparse, RRF merge, top_k, retrieval event, one-branch-fail test | `test_hybrid_runs_branches_in_parallel...` (wall-clock parallelism), `test_rrf_score_formula...` (hand-computed), `test_hybrid_degrades_when_sparse_branch_fails` (integration) | ✅ |
| Reranker: cohere_rerank reorders; rerank event with cost_estimate (defensive default) | `test_first_run_retrieves_reranks...` (before≠after ids, cost>0) + `test_cohere_cost_defaults_defensively_when_meta_missing` (never None) | ✅ (mock) / ⏳ operator |
| Dimension mismatch → EmbedderConfigError at LOAD | `test_dimension_mismatch_fails_at_load_before_any_call` (exit 2, zero HTTP calls, no artifact, both dims named) | ✅ |
| rag_hello runs end-to-end with semantic cache + hybrid retriever | full integration suite + CLI smoke over MockTransport; events all on one run_id | ✅ (mock) / ⏳ operator |

## Definition-of-done confirmation

- `uv run ruff check src/ tests/` — zero violations.
- `uv run mypy --strict src/foundry/` — passes (152 files).
- `uv run pytest tests/` — 358 passed (2a's 291 all intact).
- `run_id` threaded through embed / cache / retrieval / rerank / warning
  events and metadata.
- No secrets in code/configs/fixtures; integration test asserts none of the
  three fake vendor keys appear anywhere in the run artifact.
- Scope check: no `memory` field on `AgentSpec` (asserted by an integration
  test); `foundry.memory`, `core/node.py`, `core/function_node.py`
  untouched Phase 1 stubs; no orchestration/eval/versioning work.
