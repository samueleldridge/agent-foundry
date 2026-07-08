# Phase 2b retro

**What took longer than expected.** Retriever *wiring*, not retrieval. The
retrievers themselves (dense/sparse/hybrid, RRF, rerankers) were ~400 lines
of straightforward code with clean contracts from docs/25. The expensive
part was the compile-time story: where does a dense retriever's embedder
binding live (docs/12's `RetrieverBinding` had no `config` field), what
kind does a reranker ref resolve as (docs/25 names `catalog/cohere_rerank`
as a reranker ref but never assigns it an artifact kind), and how hybrid's
"dense retriever slot + sparse retriever slot" becomes a recursive
prepare/build pass. Three spec gaps that each forced a design decision
mid-implementation — all additive, all documented as handoff deviations,
but the docs/12 + docs/25 config sections deserve an errata pass folding
them back in.

**What changed from the plan.** (1) `ResultCache.lookup` cannot return the
tool's typed output (backends don't know schemas) — a `CachedToolResult`
envelope + dispatcher-side re-validation replaced the protocol sketch, and
the corrupted-entry path fell out for free. (2) Semantic caching is
per-agent-step (terminal response keyed by initial input), not per-LLM-call
as docs/24's diagram literally reads — replaying a cached `tool_use`
response mid-loop would re-run tools for marginal savings. (3) Cache hits
deliberately do NOT emit tool.started/completed so tool_calls.jsonl keeps
meaning "the handler ran"; the manual checklist's phrasing decided an
ambiguity the spec left open. (4) Bedrock embedder shipped as a registered
stub — SigV4 without boto3 isn't a "clean HTTP path", exactly the case the
phase prompt pre-authorised.

**What was cheaper than expected.** Fail-open. Because every backend error
is already a `CacheBackendError`/`EmbedderError` subclass, the fail-open
rule was a handful of `except FoundryError-subset → warning event + None`
sites, and the fault-injection tests (sqlite path pointed at a directory;
a corpus file deleted for the sparse branch) worked on the first run. The
2a investment in structured errors + the generic slot-wiring refactor
(`validate_connection_slot_wiring`) meant retriever/reranker slot checks
were a parameterisation, not new code. The in-process backends (SQLite +
plain-Python cosine + hand-rolled BM25) covered every exit gate without
FAISS/Redis/Postgres — optional heavy deps stayed genuinely optional.

**Friction worth flagging.** (a) The `cache.semantic.miss` event wants
`top_similarity`, but the `SemanticCache.lookup` protocol returns
`SemanticCacheHit | None` — no channel for the best-below-threshold
candidate; the backends grew a `last_top_similarity` attribute the
integration reads via `getattr`. A protocol return-shape touch-up
(`SemanticCacheLookupResult`) would be cleaner; flag for review. (b) Two
scope-key conventions now exist (semantic cache scope_key on the backend
instance; tool cache scope folded into the key by the dispatcher) —
consistent in effect, different in mechanism; worth unifying if a third
cache layer ever appears. (c) The pseudo-embedding trick in tests
(deterministic RNG seeded by text) makes identical-input similarity exactly
1.0 — great for cache-hit tests, but nothing in CI exercises a
*near*-threshold live embedding; that's inherently an operator/live-key
concern (manual test 2 covers it).

**Estimate vs actual.** One session, as planned, but the deliverable list
was the widest so far (12 items across 8 packages). The suggested commit
sequencing from the phase prompt held almost exactly (10 commits); the only
resequencing was embedders-before-schemas because `config.schemas` imports
`EmbedderBinding` from providers.
