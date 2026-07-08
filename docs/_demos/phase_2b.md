# Phase 2b demo — semantic cache + hybrid retrieval + rerank (rag_hello)

## Hero commands

```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # live-key steps — PENDING OPERATOR
export VOYAGE_API_KEY="pa-..."
export COHERE_API_KEY="..."

# Run 1: miss → hybrid retrieval → rerank → LLM → answer (+ cache store)
uv run python -m foundry run projects/rag_hello \
  --input '{"query": "what is the capital of France?"}'

# Run 2 (same input): semantic cache hit — no LLM call, instant replay
uv run python -m foundry run projects/rag_hello \
  --input '{"query": "what is the capital of France?"}'
```

## Representative output

Captured from the dev sandbox with the HTTP layer faked
(`httpx.MockTransport` serving api.anthropic.com, api.voyageai.com, and
api.cohere.com) — **no API keys exist in the sandbox**, so the answer prose
is canned; everything else (compile-time wiring + dimension checks, key
construction, sqlite cache, parallel hybrid fan-out, RRF, rerank,
events, artifacts) is the real path.

Run 1 — cold:

```text
[info] run.started            sequence=0
[info] connection             sequence=1   # acquire cohere_api (rerank stage build)
[info] agent.started          sequence=2
[info] embed                  sequence=3   # voyage:voyage-3, purpose=query (cache key)
[info] cache.semantic.miss    sequence=4   # top_similarity=0.0 threshold=0.95
[info] llm.started            sequence=5
[info] llm.completed          sequence=6   # stop_reason=tool_use
[info] cache.tool.miss        sequence=7   # local/search_docs
[info] tool.started           sequence=8
[info] retrieval              sequence=9   # kind=sparse  returned=6 (BM25 branch)
[info] embed                  sequence=10  # corpus + query embedding (dense branch)
[info] retrieval              sequence=11  # kind=dense   returned=6
[info] retrieval              sequence=12  # kind=hybrid  branch_latency_ms={dense: 4, sparse: 0}
[info] rerank                 sequence=14  # cohere_rerank cost_estimate_usd=0.002
                                           # before=[FR-001,...x6] after=[top 3, reordered]
[info] tool.completed         sequence=15  # success=true
[info] cache.tool.store       sequence=16  # ttl_s=300
[info] llm.started             ...
[info] llm.completed           ...          # stop_reason=end_turn
[info] cache.semantic.store    ...          # ttl_s=3600
[info] agent.completed / run.completed
{
  "answer": "Paris is the capital of France.",
  "sources": ["FR-001"]
}
```

Run 2 — same input, seconds later:

```text
[info] run.started            sequence=0
[info] agent.started          sequence=2
[info] embed                  sequence=3   # one query embed to build the key
[info] cache.semantic.hit     sequence=4   # similarity=1.0 threshold=0.95
                                           # saved_tokens_estimate=240
                                           # saved_cost_estimate_usd=0.0004
[info] agent.completed        sequence=5
[info] run.completed          sequence=6
{
  "answer": "Paris is the capital of France.",
  "sources": ["FR-001"]
}
```

`metadata.json`: run 1 `"llm_call_count": 2` → run 2 `"llm_call_count": 0`.

## What the demo proves

- **Layer 2 (semantic cache)**: whole agent step short-circuited on
  similarity ≥ threshold; savings audited on the hit event; a prompt bump
  (`prompts/v2.md` + pin) emits `cache.semantic.invalidate` and misses.
- **Layer 3 (tool cache)**: `search_docs` is `cacheable: true` +
  `cache_ttl_s: 300` — a second identical call (same run or within the TTL
  across runs) emits `cache.tool.hit` and never re-runs retrieval;
  tool_calls.jsonl still counts only real handler invocations.
- **Hybrid retrieval**: dense (lazily-embedded in-memory index) and sparse
  (BM25) branches fan out in parallel — the hybrid `retrieval` event
  carries both branch latencies — and merge via RRF; deleting the sparse
  corpus degrades to dense-only with a `warning` event, not a failure.
- **Rerank**: the Cohere stage reorders (`before_ids` ≠ `after_ids`),
  truncates to top 3, and always reports `cost_estimate_usd`.
- **Fail-open**: pointing the cache's sqlite path at a directory produces
  warnings and a completed run via the LLM path — exit code 0.
- **Compile-time dimension check**: editing the dense retriever's
  `dimensions: 1024 → 1536` fails at load with `EmbedderConfigError`
  naming both dims — zero HTTP calls made, no run artifact written.
- **Audit trail**: every embed / cache / retrieval / rerank / warning event
  above carries the same `run_id`.
