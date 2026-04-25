# 24 — Caching and Optimisation

## Purpose

This doc specifies the **three caching layers** the foundry supports and how they compose, plus the **embedder abstraction** that underpins semantic caching and RAG. It also documents patterns for context compression and model cascading — deferred as first-class primitives but worth naming so consumers can implement them consistently.

Caching correctly is one of the highest-leverage levers for agent pipelines in production: a well-placed semantic cache can cut LLM spend 30–70% on repetitive workloads and drop p50 latency by an order of magnitude. Caching *incorrectly* is one of the fastest ways to ship silently wrong outputs. This doc takes both halves seriously — every opt-in is gated, every hit is audited, every correctness hazard is called out explicitly.

## The three caching layers

```
    user input
        │
        ▼
   ┌──────────────────────────────────────────────────────────┐
   │  Agent step                                              │
   │                                                          │
   │  1. Build SemanticCacheKey (structural hash + embed)     │
   │  2. SemanticCache.lookup(key, threshold)                 │   ← Layer 2
   │     HIT → return cached ModelResponse (emit cache.hit)   │
   │     MISS → continue                                      │
   │                                                          │
   │  3. Provider.generate(messages, tools, settings)         │
   │     ├── Prompt-cache markers on eligible blocks           │   ← Layer 1
   │     └── Actual LLM call                                  │
   │                                                          │
   │  4. SemanticCache.store(key, response, ttl_s)            │
   │                                                          │
   │  5. Tool-call dispatch:                                  │
   │     a. hash(validated_input) → input_hash                │
   │     b. if cacheable: ResultCache.lookup(...)             │   ← Layer 3
   │        HIT → return cached output (emit cache.tool.hit)  │
   │        MISS → run handler → ResultCache.store(...)       │
   │     c. else: always run handler                          │
   └──────────────────────────────────────────────────────────┘
```

| Layer | Granularity | Opt-in | Correctness risk | Typical savings |
|---|---|---|---|---|
| **1. Prompt caching** (provider-native) | Prompt-prefix bytes | `ModelSettings.cache_control` (already specified) | None (exact match) | 30–90% input-token cost on long prompts |
| **2. Semantic caching** | Full agent-step responses | `AgentSpec.semantic_cache` (off by default) | Real — thresholds must be disciplined | 20–70% call elimination on repetitive workloads |
| **3. Tool-result caching** | Tool handler outputs | `ToolSpec.cacheable` (off by default) | None if tool is actually idempotent; real if author misjudges | Varies — huge for repeated lookups (e.g. SSI fetches) |

The three are complementary. Semantic cache hits short-circuit the LLM call. On miss, prompt caching reduces the call's cost. After the call, tool-result caching avoids duplicate tool invocations during follow-up tool turns.

## Layer 1: Prompt caching (provider-native)

Spec already in `11-provider-abstraction.md` under Anthropic-specific features. Recap of the surface for this doc:

- `ModelSettings.cache_control: CacheControlMode | None` — project-level default; applies to system prompt (`SYSTEM`), plus tool defs (`SYSTEM_AND_TOOLS`), optionally up to prior messages (`AGGRESSIVE`).
- `TextBlock.cache_control: CacheControl | None` — per-block override when `CacheControlMode` isn't granular enough.
- Capability-gated: requires `ProviderCapabilities.cache_control = True` for the bound model.
- Cache read/write tokens surfaced in `TokenUsage.cached_read_tokens` / `cached_write_tokens`.
- Cost estimation accounts for the lower cache-read price via `ModelPricing.cache_read_per_1m`.

No changes here. Just acknowledged as Layer 1.

## Layer 2: Semantic caching

### Protocol

Defined in `10-core-framework.md` — `SemanticCache` with `lookup`, `store`, `invalidate`. Key shape (`SemanticCacheKey`) includes structural hash + embedding vector; `lookup` does exact-match on structural hash + model binding + tools hash, then similarity search within that bucket.

### Configuration surface

`AgentSpec.semantic_cache: SemanticCacheConfig | None` (see `12-config-and-validation.md`). Off by default. Fields:

- `embedder_binding`: the `EmbedderBinding` used to vectorise inputs. Provider-agnostic via the registry.
- `similarity_threshold`: cosine floor for a hit. Clamped to `[0.5, 1.0]`. Default `0.95`.
- `ttl_s`: entry lifetime, 1s–30d.
- `scope`: `agent` (default) / `project` / `global`.
- `backend`: `in_process` / `redis` / `pgvector`.
- `max_entries`: LRU cap.
- `backend_config`: backend-specific (URL, table name). Secrets still via `CredentialsRef`.

### Backends

| Backend | Use case | Notes |
|---|---|---|
| `in_process` | Dev, tests, single-worker smoke | FAISS or `sqlite-vss`. Per-worker, volatile. |
| `redis` | Multi-worker prod with shared cache | Redis Stack with RediSearch vector index. Low-latency, widely deployed. |
| `pgvector` | Shared deployment that already runs Postgres | Same Postgres instance as checkpointer; one fewer service. Query latency ~5ms higher than Redis but operationally simpler. |

All three implement the same `SemanticCache` protocol from core. Adding a fourth (e.g. Qdrant, Weaviate, Pinecone) is an additive module — no core changes.

### Key construction (normative)

```python
def build_key(
    agent_spec: AgentSpec,
    agent_version: str,
    model_binding: ModelBinding,
    tools: list[ToolSchema],
    messages: list[FoundryMessage],
    embedder: Embedder,
) -> SemanticCacheKey:
    return SemanticCacheKey(
        agent_name=agent_spec.name,
        agent_version=agent_version,
        model_binding_hash=stable_hash({
            "provider": model_binding.provider,
            "model": model_binding.model,
            "temperature": model_binding.settings.temperature,
            "max_tokens": model_binding.settings.max_tokens,
            "top_p": model_binding.settings.top_p,
            "response_format": model_binding.settings.response_format,
        }),
        tools_hash=stable_hash([t.model_dump() for t in sorted(tools, key=lambda t: t.name)]),
        messages_structural_hash=stable_hash([
            {"role": m.role, "block_types": [b.type for b in m.content]}
            for m in messages
        ]),
        messages_embedding=(await embedder.embed(
            [concat_text_content(messages)],
            purpose="query",
        ))[0],
    )
```

Structural hash catches changes that shouldn't share cache entries (a new tool-use block in message history) independent of semantic content. Semantic similarity is only evaluated within the exact-match bucket of structural + model + tools.

### Correctness rules

1. **`agent_version` change invalidates everything.** Any prompt, tool-binding, or model-binding edit to the agent config (reflected in `agent_version` content hash) calls `SemanticCache.invalidate(agent_name)` at compile time before the new version serves. Cached responses against the old prompt don't leak into the new one.
2. **`model_binding_hash` separation is exact, not similar.** Two agents with nearly-identical settings but different temperatures get different cache entries. No accidental cross-temperature sharing.
3. **`tools_hash` is exact.** Adding or removing a tool changes the hash; the LLM's available affordances change, so cache separation must be strict.
4. **Threshold is per-agent, not global.** A triage agent might tolerate `0.92`. A compliance-report writer won't tolerate anything under `0.99`. Default `0.95`; operator tunes based on eval evidence.
5. **Deterministic agents benefit most; stochastic agents benefit least.** Temperature > 0 agents can see legitimate variation between acceptable outputs; caching the first one eliminates that variation. Operators should know what they're trading.
6. **Eval-driven threshold tuning is the right workflow.** Run the project's end-to-end eval with `similarity_threshold: 0.95`, record hit rate and any quality deltas. If deltas are zero and hit rate low, lower threshold and re-eval. The meta-agent can help with this loop.

### Failure modes

| Cause | Surfaced as |
|---|---|
| Embedder unavailable | `EmbedderError` — degrades gracefully (skip cache, call LLM) + warning event |
| Backend store unreachable | `CacheBackendError` — same degrade-gracefully behaviour |
| Stored entry fails schema validation | `CacheCorruptedEntry` — eviction + miss + warning |
| Stale entry returned that tests flag as wrong | caught by eval harness + threshold review; no runtime fix possible |

**Fail-safe default**: every cache error fails open (proceed without cache), logs loudly, surfaces a metric. Cache being down must never block a run.

### Observability

Every lookup and store emits a `foundry.cache.semantic` event with dimensions: `agent`, `event` (hit/miss/store/invalidate), `similarity` (on hit), `threshold`, `ttl_s` (on store), `saved_tokens_estimate` (on hit, computed from the cached response's `TokenUsage`), `saved_cost_usd` (on hit).

The audit trail then answers in a single query: "what's the hit rate on this agent this week? How many $ did semantic caching save?"

## Layer 3: Tool-result caching

### Protocol

`ResultCache` (in `10-core-framework.md`). Exact-match by hash of validated input. Simpler than semantic caching — no similarity search, no embedder, no threshold.

### Configuration

`ToolSpec` gains three fields (see `12-config-and-validation.md`):

- `cacheable: bool = False` — opt-in per tool. Default off because silent caching of non-idempotent tools is a correctness bug.
- `cache_ttl_s: int | None = None` — required when `cacheable=True`. Tuned per tool based on source staleness (minutes to hours typical).
- `cache_scope: "agent" | "project" | "global"` — isolation. Default `project`.

Model-validator rule: `cacheable` and `cache_ttl_s` must be consistent (both set or both unset). Enforced at load.

### When to cache a tool

✅ Safe: tools that read reference data which is stable on the cache-TTL timescale (SSI fetches, employee directory lookups, legal-entity identifiers, product catalogue, historical trade records).

❌ Never: tools that read live state (current pricing, queue depth, ticket statuses), tools that write (send_email, trigger_rpa), tools whose output depends on time or non-input context (weather, random, auth-token-dependent).

⚠️ Careful: tools that read mostly-stable data with occasional updates (runbooks, compliance rules). Safe under short TTL; problematic under long TTL. Prefer short TTL + eval coverage that can detect staleness.

### Backends

Share storage with semantic cache where possible:
- `in_process` (SQLite)
- `redis` (string keys, SETEX for TTL)
- `pgvector` Postgres (same DB, different table; no vector index needed)

### Observability

`foundry.cache.tool` events with dimensions `tool_ref`, `tool_version`, `event` (hit/miss/store), `input_hash`, `cached_at` (on hit). Aggregate hit-rate per tool is a trivial audit query.

## Embedder abstraction (shared across Layer 2 and RAG)

`Embedder` protocol specified in `10-core-framework.md`; concrete implementations in `foundry.providers.embedders` (see `11-provider-abstraction.md`).

Configuration: `EmbedderBinding` (provider + model + settings + credentials_ref). Same pattern as `ModelBinding`.

Supported vendors: Voyage (recommended for Anthropic deployments), OpenAI, Cohere, Bedrock (Titan, Cohere-on-Bedrock). Vertex Gemini embeddings added in Phase 1 polish if time permits.

Provider-agnosticism is real: swap `embedder_binding.provider: "voyage" → "openai"` in a config and the semantic cache continues to function (modulo re-indexing from scratch because dimensions and embedding semantics differ across vendors).

### Dimension compatibility

Semantic cache stores use a fixed vector dimension. Changing embedder models mid-life requires either:
- New cache backend table/namespace for the new dimension, or
- `SemanticCache.invalidate("*")` wipe + re-embed on hit attempts (fail-open to LLM call).

The compile-time capability check enforces dimension agreement between `embedder_binding` and the backend's configured dimension. Mismatch → `EmbedderConfigError`.

### Asymmetric query/document embeddings

Vendors like Voyage and Cohere support asymmetric embeddings — "is this a query?" vs "is this a document?" produces different vectors optimised for retrieval. `Embedder.embed(inputs, purpose="query" | "document")`.

For semantic caching, both the stored key and the lookup key are embedded with `purpose="query"` (they're both "what is the agent being asked?"). For RAG retrieval, documents are embedded with `purpose="document"` at ingest time and queries with `purpose="query"` at retrieval time. `supports_query_document_split` in `EmbedderCapabilities` indicates whether the vendor actually differentiates; symmetric-only embedders treat both identically.

## Context compression (pattern, not primitive)

Long-running agents accumulate message history that eats tokens. Options, in increasing complexity:

- **Sliding window**: keep last N messages; summarise older into a single synthetic message. Implementable as a `before_node` `LifecycleHook` that projects state.
- **Recursive summarisation**: periodically summarise the conversation, replace detailed history with the summary. Implemented as a dedicated agent node that writes to state.
- **Hierarchical memory**: recent messages verbatim, medium-term summarised, long-term vector-indexed and retrieved on demand. Combines this doc with `25-retrieval-and-rag.md`.

Not a first-class primitive in v1. Documented here so implementations across projects stay consistent. Promote to a primitive if three-plus projects build their own variant.

## Model cascading (pattern, not primitive)

Common cost-reduction pattern: run a cheap model (Haiku) first; only escalate to Opus if confidence is low or the task is genuinely hard. 30–60% cost reduction typical for triage workloads.

Implementable today via orchestration — a supervisor routes to `worker_haiku` by default; on low-confidence output, re-routes to `worker_opus`. Fully configurable via existing `flow.type: graph` conditional edges.

Helper primitive `CascadedProvider` deferred. Design sketch:

```python
class CascadedProvider(ProviderAdapter):
    primary: Provider       # e.g. claude-haiku
    fallback: Provider      # e.g. claude-opus
    should_escalate: Callable[[ModelResponse], bool]
    # Default: escalate if logprobs / confidence below threshold,
    # if output fails output_schema validation, or on timeout.
```

Not shipping in v1; the orchestration layer's conditional edges cover the use case with explicit topology.

## Composition with multi-worker / multi-host deployment

From `85-batch-and-throughput.md`:

- **In-process cache**: per-worker, volatile, fine for single-worker dev.
- **Redis / pgvector caches**: shared across workers and hosts. Same read-your-own-writes guarantees as the underlying store.
- **Consistency window**: writes to Redis are effectively immediate; pgvector writes respect Postgres transaction visibility rules.

For batch submission (`POST /batch`), the shared cache is load-bearing — 8 workers processing 50k items with semantic cache dedup across them is where the value is.

For streaming runs, cache hits are observable via the `RunEvent` stream (`cache.semantic.hit` event before the absent `llm.started`). Clients can differentiate cached vs freshly-generated responses via the event shape.

## Observability reference (recap)

Events emitted (full attribute shape in `10-core-framework.md` § Streaming events, summary in `01-architecture-overview.md` § Observability):

- `foundry.embed` — every embedder call.
- `foundry.cache.semantic` — hit/miss/store/invalidate on the semantic cache.
- `foundry.cache.tool` — hit/miss/store on the tool-result cache.

Derived metrics (surface in `foundry obs`):

- `foundry.cache.semantic.hit_rate` — by agent, by project, by day.
- `foundry.cache.semantic.saved_cost_usd` — cumulative.
- `foundry.cache.semantic.saved_tokens` — cumulative.
- `foundry.cache.tool.hit_rate` — by tool.
- `foundry.embed.cost_usd` — by provider, by model.

All of these feed the `foundry obs` CLI + whatever Grafana/Langfuse/etc. backend the user pipes OTel into.

## Invariants (normative)

1. **Semantic caching is off by default.** `AgentSpec.semantic_cache: SemanticCacheConfig | None = None`. Requires explicit opt-in with embedder binding + threshold.
2. **Tool caching is off by default.** `ToolSpec.cacheable: bool = False`. Requires explicit opt-in with TTL.
3. **Agent-version change invalidates the semantic cache for that agent.** Enforced at compile time.
4. **Cache failures fail open.** Any `CacheBackendError` degrades to "skip cache, call LLM" plus a loud metric — never blocks a run.
5. **Every cache lookup, store, and invalidate emits a `RunEvent`.** No silent cache activity in the audit trail.
6. **Embedder vendor changes require cache re-index or namespace separation.** The compile check refuses mismatched dimensions.
7. **Semantic cache `lookup` matches structural + model + tools exactly, then similarity.** No cross-bucket similarity hits.

## Test expectations

### Unit

1. **Key construction is deterministic.** Same inputs → same `SemanticCacheKey` (structural fields); embedding vectors equal modulo float tolerance.
2. **Exact-match bucket separation.** Same messages but different tools_hash → cache miss, not false hit.
3. **Threshold enforcement.** Similarity 0.94 with threshold 0.95 → miss; 0.96 → hit.
4. **Agent-version invalidation.** Cache entry for `agent_v1`; bump to `v2`; lookup returns miss + invalidate event.
5. **Tool cache validator.** `cacheable=True, cache_ttl_s=None` → `ValueError` at load.
6. **Cache failure fails open.** Patched backend raises `CacheBackendError`; agent call completes using the LLM path + warning event.

### Integration (Phase 2 exit gate extension)

1. End-to-end: an agent with `semantic_cache.backend: in_process` hits cache on a re-run of the same input; emitted events show hit + saved_cost.
2. A tool with `cacheable: true` returns cached output on the second call in the same run; emitted events show hit.
3. An agent-version bump invalidates the semantic cache; the next call with the same input is a miss.

## Open questions

1. **Cache-warmup workflow.** Should there be a `foundry cache warmup <project> --eval <set>` that runs the eval set, populating the semantic cache, so the first real deployment doesn't pay cold-miss taxes? Lean: yes, Phase 9 tooling. Useful for batch deployments that replay a sample to warm cache before going live.
2. **Prompt-cache + semantic-cache interaction telemetry.** Both save cost on the same call. Should the audit trail try to attribute savings across layers? Probably not — additive savings are a simple sum in reports. Mark as a report-layer concern.
3. **Cache key for streaming responses.** Streaming calls accumulate a final ModelResponse; we cache the final response, not the deltas. Playback on cache hit is instantaneous (synthesised `LLMDelta`s from the cached blocks). Clients should handle both code paths the same. Worth surfacing as a doc note.
4. **Cross-agent cache sharing when safe.** Two agents with identical prompts + model + tools (e.g. different project versions) could share cache. Current design says no (scope is `agent` or `project`). Reconsider if there's a real gain; probably small.
