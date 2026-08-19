# Phase 2b — Manual Smoke Tests

**Phase scope**: `foundry.core.embedder` + `foundry.core.retrieval` + `foundry.core.cache` + cache/retriever config schemas + `foundry.providers.embedders` (Voyage + OpenAI + Cohere; Bedrock registered stub) + `foundry.cache` (in_process for real; redis/pgvector shapes) + `foundry.retrieval` (dense / sparse / hybrid + rerankers) + catalog retriever templates + `projects/rag_hello` demonstrating cache + hybrid retriever end-to-end.

**Reference**: [docs/03-development-phases.md § Phase 2b](../03-development-phases.md) exit gate; [docs/_phase_handoffs/phase_2b.md](../_phase_handoffs/phase_2b.md) for implementation notes and deviations (the example project is **rag_hello**; all backends are **in_process** — no Postgres/Redis needed; the embedder is **voyage-3**, the reranker **Cohere**).

## Preconditions

- Phase 2a manual smoke test fully signed off.
- Claude Code review session for Phase 2b has reported **PASS**.
- Working tree clean.
- API keys:
  - `ANTHROPIC_API_KEY` (LLM)
  - `VOYAGE_API_KEY` (embedder — semantic cache + dense retriever)
  - `COHERE_API_KEY` (reranker — skip-with-note if you don't have it; without it Test 11 still runs but the rerank stage falls through with a warning)
  - `OPENAI_API_KEY` only for Test 1's second embedder.
- No infra needed: caches are SQLite files under `~/.foundry/cache/`
  (`semantic.db`, `tool_results.db`). **Delete them between test attempts
  to reset cache state**: `rm -f ~/.foundry/cache/*.db`

## Setup

```bash
cd <repo-root>
export ANTHROPIC_API_KEY="sk-ant-..."
export VOYAGE_API_KEY="..."
export COHERE_API_KEY="..."

ls projects/            # includes rag_hello/
ls catalog/retrievers/  # pgvector_dense, hybrid_rrf, cohere_rerank
INPUT='{"query": "what is the capital of France?"}'
```

## Tests

### Test 1 — Embedder round-trip (Voyage + OpenAI)

**What we're verifying**: live calls to two embedder providers produce embeddings of the advertised dimensionality.

**Run**:

```bash
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
        out = await e.embed(["hello world"], "query")
        print(f"{cfg.provider}/{cfg.model}: dim={len(out[0].vector)} "
              f"advertised={e.capabilities.dimensions} "
              f"tokens={out[0].input_tokens} cost={out[0].cost_estimate_usd}")

asyncio.run(main())
PY
```

**Expected**:
- Both succeed; voyage-3 → 1024, text-embedding-3-small → 1536; `dim == advertised`.
- (The adapter hard-fails with `EmbedderUnexpectedError` if the provider's dims ever drift from the manifest — so success here IS the dim check.)

**If it fails**: HTTP 401 → key not exported. `EmbedderConfigError` → registry/manifest problem; fix session.

- [ ] Pass

### Test 2 — Semantic cache hit on re-run

**What we're verifying**: with `semantic_cache.backend: in_process`, the same input re-run hits cache (no LLM call) and emits `cache.semantic.hit` with `similarity ≥ threshold` and savings populated.

**Run**:

```bash
rm -f ~/.foundry/cache/*.db
uv run python -m foundry run projects/rag_hello --input "$INPUT"
RUN1=$(ls -t ~/.foundry/runs/ | head -1)
uv run python -m foundry run projects/rag_hello --input "$INPUT"
RUN2=$(ls -t ~/.foundry/runs/ | head -1)

jq 'select(.event | startswith("cache.semantic"))' ~/.foundry/runs/$RUN2/events.jsonl
jq '.llm_call_count' ~/.foundry/runs/$RUN1/metadata.json ~/.foundry/runs/$RUN2/metadata.json
```

**Expected**:
- Run 2 emits `cache.semantic.hit` with `similarity ≥ 0.95` (identical input → ~1.0), non-zero `saved_cost_estimate_usd` and `saved_tokens_estimate`.
- `llm_call_count`: run 1 = 2, run 2 = 0.

**If it fails**: no hit → embedder returned different vectors for identical text (provider-side nondeterminism; retry or tune threshold) or the cache file was wiped between runs. Hit with zero savings → cost-attribution wiring incomplete; fix session.

- [ ] Pass

### Test 3 — Semantic cache invalidates on prompt-version bump

**What we're verifying**: the agent-version content hash covers the prompt; bumping it emits `cache.semantic.invalidate` and misses.

**Run** (immediately after Test 2, same cache files):

```bash
AGENT=projects/rag_hello/agents/rag_agent
cp $AGENT/prompts/v1.md $AGENT/prompts/v2.md
echo "Always answer in exactly one sentence." >> $AGENT/prompts/v2.md
# pin v2 in agent.yaml — ONLY the prompt block (the retriever bindings
# also say `version: v1`; the prompt's line is the only 2-space-indented one):
sed -i '' 's|^  version: v1$|  version: v2|; s|prompts/v1.md|prompts/v2.md|' $AGENT/agent.yaml

uv run python -m foundry run projects/rag_hello --input "$INPUT"
RUN3=$(ls -t ~/.foundry/runs/ | head -1)
jq 'select(.event | contains("cache.semantic"))' ~/.foundry/runs/$RUN3/events.jsonl
jq '.llm_call_count' ~/.foundry/runs/$RUN3/metadata.json

git checkout -- projects/rag_hello/ && rm $AGENT/prompts/v2.md
```

**Expected**:
- One `cache.semantic.invalidate` with `reason: agent_version_changed` and differing previous/current versions, then `cache.semantic.miss`, then a store.
- `llm_call_count > 0` (the LLM really ran).

**If it fails**: prompt not reflected in the version hash → fix session referencing docs/24 § Key construction + correctness rule 1.

- [ ] Pass

### Test 4 — Tool-result cache hit within TTL

**What we're verifying**: `search_docs` (`cacheable: true`, `cache_ttl_s: 300`) returns cached output on a second identical call. NOTE: on a same-input re-run the SEMANTIC cache hits first (no tool call at all), so disable it for this test.

**Run**:

```bash
rm -f ~/.foundry/cache/*.db
# temporarily disable the semantic cache (keep the block, flip the switch):
#   semantic_cache:
#     enabled: false
perl -pi -e 's|^semantic_cache:|semantic_cache:\n  enabled: false|' \
  projects/rag_hello/agents/rag_agent/agent.yaml

uv run python -m foundry run projects/rag_hello --input "$INPUT"
uv run python -m foundry run projects/rag_hello --input "$INPUT"   # within 300s
RUN=$(ls -t ~/.foundry/runs/ | head -1)
jq 'select(.event | startswith("cache.tool"))' ~/.foundry/runs/$RUN/events.jsonl
wc -l ~/.foundry/runs/$RUN/tool_calls.jsonl 2>/dev/null || echo "no tool_calls.jsonl"

git checkout -- projects/rag_hello/
```

**Expected**: run 2 emits `cache.tool.hit` for `local/search_docs`; it has NO `tool_calls.jsonl` entries (cache hits short-circuit before `tool.started` — the handler never ran) and no `retrieval` events. (If the LLM phrased the tool input differently across runs the exact-match key changes — retry; the deterministic version of this gate is pinned by the integration suite.)

- [ ] Pass

### Test 5 — Tool-cache validator (adversarial)

**What we're verifying**: `cacheable: true` WITHOUT `cache_ttl_s` is rejected at load.

**Run**:

```bash
sed -i '' '/cache_ttl_s: 300/d' projects/rag_hello/tools/search_docs/v1/tool.yaml
uv run python -m foundry run projects/rag_hello --input "$INPUT" ; echo "exit=$?"
git checkout -- projects/rag_hello/
```

**Expected**: exit=2; `ConfigValidationError` naming the tool.yaml file and explaining that cacheable tools must set `cache_ttl_s`; no new run directory under `~/.foundry/runs/`.

- [ ] Pass

### Test 6 — Cache failure fails open (fault injection)

**What we're verifying**: a broken cache backend degrades to the LLM path with warnings — never blocks.

**Run** (point the sqlite backend at a directory, which can't open as a DB):

```bash
mkdir -p /tmp/not_a_db
# add under the semantic_cache block in agent.yaml:
#   backend_config:
#     path: /tmp/not_a_db
perl -pi -e 's|^  backend: in_process|  backend: in_process\n  backend_config:\n    path: /tmp/not_a_db|' \
  projects/rag_hello/agents/rag_agent/agent.yaml

uv run python -m foundry run projects/rag_hello --input "$INPUT" ; echo "exit=$?"
RUN=$(ls -t ~/.foundry/runs/ | head -1)
jq 'select(.event == "warning")' ~/.foundry/runs/$RUN/events.jsonl
jq '.llm_call_count' ~/.foundry/runs/$RUN/metadata.json

git checkout -- projects/rag_hello/
```

**Expected**: exit=0 (NOT an exception); `warning` events with `category: cache.semantic.error` (version-marker, lookup, and store all fail open — up to 3); `llm_call_count > 0`. A non-zero exit here is a P0 reliability bug — fix session.

- [ ] Pass

### Test 7 — Hybrid retriever runs dense + sparse in parallel + RRF merge

**Run** (inspect any run that actually called the tool, e.g. Test 4's first run):

```bash
jq 'select(.event == "retrieval")' ~/.foundry/runs/<run_with_tool_call>/events.jsonl
```

**Expected**:
- Three events: `kind: dense`, `kind: sparse`, and `kind: hybrid` (the branches emit their own events; the hybrid event is the merged one).
- The hybrid event carries `branch_latency_ms` with BOTH `dense` and `sparse` keys and `branches_failed: []`; overall latency tracks the slower branch, not the sum (wall-clock parallelism is also pinned by a unit test).

- [ ] Pass

### Test 8 — Hybrid retriever degrades on one-branch failure

**Run** (break the sparse branch's corpus):

```bash
# Edit projects/rag_hello/agents/rag_agent/agent.yaml by hand: under the
# retriever config's `sparse:` block change
#   corpus_path: corpus.json  ->  corpus_path: missing.json
uv run python -m foundry run projects/rag_hello --input "$INPUT" ; echo "exit=$?"
RUN=$(ls -t ~/.foundry/runs/ | head -1)
jq 'select(.event == "warning" or .event == "retrieval")' ~/.foundry/runs/$RUN/events.jsonl
git checkout -- projects/rag_hello/
```

**Expected**: exit=0; a `warning` with `category: retrieval.branch_failed` naming the sparse branch; the hybrid `retrieval` event shows `branches_failed: ["sparse"]` and `returned > 0` (dense results flowed through).

- [ ] Pass

### Test 9 — Reranker emits cost event

**Run** (skip with note if no Cohere key):

```bash
jq 'select(.event == "rerank")' ~/.foundry/runs/<run_with_tool_call>/events.jsonl
```

**Expected**:
- `before_ids` (up to 8 candidates) ≠ `after_ids` (3, reordered by relevance) — some reordering happened.
- `cost_estimate_usd` present and > 0. It is NEVER null: the adapter defaults defensively when the provider reports no billing units.

- [ ] Pass (or [ ] Skipped: no Cohere key)

### Test 10 — Dimension mismatch compile check (adversarial)

**Run**:

```bash
sed -i '' 's|dimensions: 1024|dimensions: 1536|' \
  projects/rag_hello/agents/rag_agent/agent.yaml
uv run python -m foundry run projects/rag_hello --input "$INPUT" ; echo "exit=$?"
git checkout -- projects/rag_hello/
```

**Expected**: exit=2 AT LOAD — `EmbedderConfigError` naming both dims (1024 vs 1536) and the disagreeing artifacts (voyage/voyage-3 vs the retriever config); no embedding or LLM call made; no run directory created.

- [ ] Pass

### Test 11 — Second example project end-to-end (hero demo)

**Run**:

```bash
rm -f ~/.foundry/cache/*.db
uv run python -m foundry run projects/rag_hello --input "$INPUT"
uv run python -m foundry run projects/rag_hello --input "$INPUT"
```

**Expected**:
- Run 1: coherent answer citing corpus ids (e.g. `FR-001`); the artifact tells the story miss → retrieval (dense + sparse + hybrid) → rerank → LLM → stores — all events tagged with the same `run_id`.
- Run 2: `cache.semantic.hit`, same answer, `llm_call_count: 0`.

- [ ] Pass

### Test 12 — Scope leakage check (Phase 2c content NOT here)

**What we're verifying**: no memory/FunctionNode RUNTIME snuck in. (Note: `core/memory.py`, `core/node.py`, `core/function_node.py`, and the `FunctionNodeSpec` schema are Phase 1 protocol stubs and are SUPPOSED to exist; what must not exist is a `memory` field on AgentSpec or concrete memory/function-node implementations.)

**Run**:

```bash
uv run python - <<'PY'
from foundry.config import AgentSpec
assert "memory" not in AgentSpec.model_fields, "2c leakage: AgentSpec.memory"
print("Clean — no memory field on AgentSpec")
PY
head -3 src/foundry/memory/coordinator.py   # still a one-line stub docstring
```

**Expected**: "Clean"; `foundry/memory/*` unchanged stubs.

- [ ] Pass

### Test 13 — Phase 2a regression check

```bash
export HELLO_SERVICE_API_KEY=dummy
uv run python -m foundry run projects/hello --input '{"name": "world"}'
```

**Expected**: same behaviour as at end of Phase 2a. Greeting produced; no errors.

- [ ] Pass

### Test 14 — Commit hygiene

Same as prior phases: conventional commits, no co-author lines, no secrets in the diff, logical chunks.

- [ ] Pass

## Sign-off

- [ ] All 14 tests passed (Test 9 may be skipped with note).
- [ ] Phase 2a still works (regression-clean).
- [ ] No Phase 2c content leaked into 2b.
- [ ] Ready to start Phase 2c.

Signed off: ____________________ Date: __________

Notes for `docs/_retros/phase_2b.md`: especially anything about Test 6 (cache fail-open) and Test 8 (hybrid degrade) — those are reliability stories that will compound through later phases.
