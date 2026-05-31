# Phase 2b — Manual Smoke Tests

**Phase scope**: `foundry.core.embedder` + `foundry.core.retrieval` + `foundry.core.cache` + cache/retriever config schemas + `foundry.providers.embedders` (Voyage + OpenAI + Cohere + Bedrock) + `foundry.cache` (in_process / redis / pgvector) + `foundry.retrieval` (dense / sparse / hybrid + rerankers) + catalog retriever templates + a second example project demonstrating cache + hybrid retriever end-to-end.

**Reference**: [docs/03-development-phases.md § Phase 2b](../03-development-phases.md) exit gate; [docs/_phase_handoffs/phase_2b.md](../_phase_handoffs/phase_2b.md) for implementation notes (especially which example project name was used and which embedder + vector store were seeded).

## Preconditions

- Phase 2a manual smoke test fully signed off.
- Claude Code review session for Phase 2b has reported **PASS**.
- Working tree clean.
- API keys needed (export the ones the example project uses):
  - `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` (LLM)
  - `VOYAGE_API_KEY` and/or `OPENAI_API_KEY` (embedders)
  - `COHERE_API_KEY` (reranker — skip-with-note if you don't have it)
- A running `pgvector`-enabled Postgres if the example project uses it (or use the `in_process` backend for the cache test if not). The handoff note should specify which.

## Setup

```bash
cd /Users/sam/projects/agent-foundry
export ANTHROPIC_API_KEY="sk-ant-..."
export VOYAGE_API_KEY="..."
# etc.

# Confirm second example project exists
ls projects/  # should now include rag_hello/ or similar
ls catalog/retrievers/  # should include pgvector_dense + hybrid_rrf
```

## Tests

### Test 1 — Embedder round-trip (Voyage + OpenAI)

**What we're verifying**: live calls to two embedder providers produce embeddings of the correct dimensionality.

**Run**:

```bash
# Use a small helper script or the CLI if one was added
# Suggested approach: short Python REPL or one-shot using foundry's embedder API
uv run python - <<'PY'
import asyncio
from foundry.providers.embedders import load_embedder
from foundry.config.schemas import EmbedderBinding

async def main():
    for cfg in [
        EmbedderBinding(provider="voyage", model="voyage-3"),
        EmbedderBinding(provider="openai", model="text-embedding-3-small"),
    ]:
        e = load_embedder(cfg)
        out = await e.embed(["hello world"])
        print(f"{cfg.provider}/{cfg.model}: dim={len(out[0].vector)} capabilities={e.capabilities}")

asyncio.run(main())
PY
```

**Expected**:
- Both calls succeed.
- The dim printed matches the model's advertised dimensionality (e.g., voyage-3 → 1024, text-embedding-3-small → 1536).
- No traceback.

**If it fails**:
- HTTP error → API key not set.
- Dim mismatch → adapter not exposing the right `EmbedderCapabilities`; fix session.

- [ ] Pass

### Test 2 — Semantic cache hit on re-run

**What we're verifying**: with `semantic_cache.backend: in_process`, running the same input twice causes the second to hit cache (no LLM call) and emit a `cache.semantic.hit` event with similarity ≥ threshold.

**Run**:

```bash
# Edit projects/<rag_example>/agents/<agent>/agent.yaml: ensure
# semantic_cache is configured with backend: in_process
INPUT='{"query": "what is the capital of France?"}'

# First run (populates cache)
uv run python -m foundry run projects/<rag_example> --input "$INPUT"
RUN1=$(ls -t ~/.foundry/runs/ | head -1)

# Second run (should hit cache)
uv run python -m foundry run projects/<rag_example> --input "$INPUT"
RUN2=$(ls -t ~/.foundry/runs/ | head -1)

# Confirm
jq 'select(.event_type | startswith("cache.semantic"))' ~/.foundry/runs/$RUN2/events.jsonl
diff <(jq -r .llm_call_count ~/.foundry/runs/$RUN1/metadata.json) \
     <(jq -r .llm_call_count ~/.foundry/runs/$RUN2/metadata.json)
```

**Expected**:
- Run 2 emits `cache.semantic.hit` with `similarity ≥ threshold` and a non-zero `saved_cost_usd`.
- `llm_call_count` for Run 2 is lower than Run 1.

**If it fails**:
- No hit → similarity threshold may be too strict; tune in the agent config or fix session.
- Hit but `saved_cost_usd` is 0 or missing → cost-attribution wiring incomplete.

- [ ] Pass

### Test 3 — Semantic cache invalidates on prompt-version bump

**What we're verifying**: cache keys include the prompt version; bumping the version invalidates prior entries.

**Run**:

```bash
# Add v2 of the agent's prompt (copy v1, edit slightly)
cp projects/<rag_example>/agents/<agent>/prompts/v1.md \
   projects/<rag_example>/agents/<agent>/prompts/v2.md
# (Edit v2 to differ from v1 — e.g., add a system instruction)

# Update agent.yaml to pin v2
# Run with same input as Test 2
uv run python -m foundry run projects/<rag_example> --input "$INPUT"
RUN3=$(ls -t ~/.foundry/runs/ | head -1)

# Inspect
jq 'select(.event_type | contains("cache"))' ~/.foundry/runs/$RUN3/events.jsonl

# Revert
git checkout -- projects/<rag_example>/
rm projects/<rag_example>/agents/<agent>/prompts/v2.md
```

**Expected**:
- Run 3 emits a `cache.semantic.invalidate` (or `miss`) event.
- An actual LLM call happens (`llm_call_count > 0`).

**If it fails**: prompt version not in cache key → SemanticCacheKey shape is wrong; fix session referencing `docs/24 § Key construction`.

- [ ] Pass

### Test 4 — Tool-result cache hit within TTL

**What we're verifying**: a tool with `cacheable: true` + `cache_ttl_s: 60` returns cached output on a second call within the TTL.

**Run**:

```bash
# Use a tool that has cacheable: true + cache_ttl_s set
# (Either the seeded catalog tool with these fields added, or a
# project-local tool the impl session created for this test.)
# Trigger the agent to call the tool twice in one run (or two runs
# within 60s).
uv run python -m foundry run projects/<example_with_cacheable_tool> --input '...'
jq 'select(.event_type == "cache.tool.hit")' ~/.foundry/runs/<latest>/events.jsonl
```

**Expected**: `cache.tool.hit` event on the second call; only one actual tool invocation in `tool_calls.jsonl`.

**If it fails**: result cache not wired into dispatcher; fix session.

- [ ] Pass

### Test 5 — Tool-cache validator (adversarial)

**What we're verifying**: setting `cacheable: true` WITHOUT `cache_ttl_s` is rejected at config load.

**Run**:

```bash
# Pick a tool's tool.yaml and add cacheable: true with NO cache_ttl_s
# Try to load the project
uv run python -m foundry run projects/<example> --input '{}' ; echo "exit=$?"
# Revert
git checkout -- <path>/tool.yaml
```

**Expected**: non-zero exit; `ConfigValidationError` (or similar) names the file, the field, and explains that `cacheable: true` requires `cache_ttl_s`.

**If it fails**: validator missing → fix session.

- [ ] Pass

### Test 6 — Cache failure fails open (fault injection)

**What we're verifying**: when the cache backend raises (e.g., Redis down), the run completes via the LLM path and emits a warning event — never blocks.

**Run**:

```bash
# Option A (cleanest): the impl session ships a test-only "always raises"
# semantic cache backend. Configure agent.yaml to use it.
# Option B: if you have Redis and the example uses it, stop redis briefly:
#   docker stop <redis-container> (or similar)

uv run python -m foundry run projects/<rag_example> --input '...'
jq 'select(.event_type | contains("cache") and contains("error"))' ~/.foundry/runs/<latest>/events.jsonl
```

**Expected**:
- Exit code 0 (NOT an exception).
- A `cache.error` (or warning) event captured the backend failure.
- The LLM was actually called (`llm_call_count > 0`).

**If it fails**: cache failure killed the run → fail-open is broken; this is a P0 reliability bug. Fix session.

- [ ] Pass

### Test 7 — Hybrid retriever runs dense + sparse in parallel + RRF merge

**What we're verifying**: `hybrid_rrf` retriever fans out dense + sparse, merges via RRF, returns top_k. A `retrieval` event names both branches.

**Run**:

```bash
uv run python -m foundry run projects/<rag_example> --input '...'
jq 'select(.event_type == "retrieval")' ~/.foundry/runs/<latest>/events.jsonl
```

**Expected**:
- A `retrieval` event with `kind: hybrid_rrf` (or similar) containing per-branch latencies and the merged top_k.
- Both branches show non-zero latency in roughly the same time window (proves parallel, not sequential).

**If it fails**: branches ran sequentially → fix session to use `asyncio.gather` or equivalent.

- [ ] Pass

### Test 8 — Hybrid retriever degrades on one-branch failure

**What we're verifying**: if dense or sparse fails, the other returns + a warning is emitted; the run does not abort.

**Run**:

```bash
# Cause one branch to fail. Easiest: misconfigure the sparse retriever
# (e.g., point it at a non-existent Elasticsearch host) while leaving
# dense functional.
uv run python -m foundry run projects/<rag_example> --input '...'
jq 'select(.event_type | contains("retrieval"))' ~/.foundry/runs/<latest>/events.jsonl
git checkout -- projects/<rag_example>/
```

**Expected**: warning event names the failed branch; the other branch's results made it into the merged output; run completed.

**If it fails**: one failure aborted the retrieval → fix session.

- [ ] Pass

### Test 9 — Reranker emits cost event

**What we're verifying**: `cohere_rerank` reorders docs and emits a `rerank` event with `cost_estimate` populated.

**Run** (skip with note if no Cohere key):

```bash
uv run python -m foundry run projects/<rag_example_with_reranker> --input '...'
jq 'select(.event_type == "rerank")' ~/.foundry/runs/<latest>/events.jsonl
```

**Expected**:
- `rerank` event with `before_order` ≠ `after_order` (some reordering happened).
- `cost_estimate` field present and > 0.

**If it fails**: cost_estimate missing → adapter not populating it; fix session (the spec is strict that this must default to 0 if the adapter can't compute, never None).

- [ ] Pass (or [ ] Skipped: no Cohere key)

### Test 10 — Dimension mismatch compile check (adversarial)

**What we're verifying**: configuring a dense retriever whose embedder dim ≠ vector-store configured dim fails at load with `EmbedderConfigError`.

**Run**:

```bash
# Edit a retriever's binding so the embedder produces e.g. 1024-d
# vectors but the vector store is configured for 1536-d.
uv run python -m foundry run projects/<rag_example> --input '...' ; echo "exit=$?"
git checkout -- projects/<rag_example>/
```

**Expected**:
- Non-zero exit AT LOAD (no LLM call, no embedding call made).
- `EmbedderConfigError` names both dims and the artifacts that disagree.

**If it fails**:
- Error at first call instead of load → dim check is in the wrong stage; fix session.

- [ ] Pass

### Test 11 — Second example project end-to-end (hero demo)

**What we're verifying**: the rag_hello (or equivalent) project demonstrates cache + hybrid retriever in one cohesive demo.

**Run**:

```bash
uv run python -m foundry run projects/<rag_example> --input '{"query": "your query"}'
```

**Expected**:
- Output is a coherent answer drawing on the retrieved + reranked context.
- First run: cache miss → retrieval → rerank → LLM → answer.
- Second run with same input: cache hit (probably) → answer (cheaper).
- The run artifact tells a clean story: events for retrieval, rerank, cache, llm — all tagged with the same `run_id`.

**If it fails**: the project as configured doesn't actually exercise the new primitives; ask the impl session to harden the demo.

- [ ] Pass

### Test 12 — Scope leakage check (Phase 2c content NOT here)

**What we're verifying**: no memory layer or FunctionNode code snuck in.

**Run**:

```bash
ls src/foundry/memory/ src/foundry/core/memory.py \
   src/foundry/core/function_node.py src/foundry/core/node.py \
   2>&1 | grep -v 'No such'

grep -nE 'memory:|FunctionNodeSpec' src/foundry/config/schemas.py || echo "Clean — no memory or FunctionNode in schemas yet"
```

**Expected**: only "No such file" errors (file checks come back empty); grep returns "Clean".

**If it fails**: out-of-scope leakage; fresh review session to confirm.

- [ ] Pass

### Test 13 — Phase 2a regression check

**What we're verifying**: Phase 2b changes didn't break Phase 2a's hello_agent demo.

**Run**:

```bash
uv run python -m foundry run projects/hello --input '{"name": "world"}'
```

**Expected**: same behavior as at end of Phase 2a. Greeting produced. No errors.

**If it fails**: regression — fix before moving on.

- [ ] Pass

### Test 14 — Commit hygiene

Same as prior phases.

- [ ] Pass

## Sign-off

- [ ] All 14 tests passed (Test 9 may be skipped with note).
- [ ] Phase 2a still works (regression-clean).
- [ ] No Phase 2c content leaked into 2b.
- [ ] Ready to start Phase 2c.

Signed off: ____________________ Date: __________

Notes for `docs/_retros/phase_2b.md`: especially anything about Test 6 (cache fail-open) and Test 8 (hybrid degrade) — those are reliability stories that will compound through later phases.
