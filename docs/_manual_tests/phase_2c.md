# Phase 2c — Manual Smoke Tests

**Phase scope**: `foundry.core.node` + `foundry.core.function_node` + `foundry.core.memory` + memory + FunctionNode config schemas + `foundry.memory` (DefaultMemory + 3 layers + prompt assembly) + remaining compile-time validation (namespace collision, mixed-flow refs) + a third example project demonstrating memory layers + a FunctionNode in a sequential flow. **Completing this sub-phase closes out Phase 2.**

**Reference**: [docs/03-development-phases.md § Phase 2c](../03-development-phases.md) exit gate; [docs/_phase_handoffs/phase_2c.md](../_phase_handoffs/phase_2c.md) for implementation notes (especially which third example project was built and how memory layers are configured).

## Preconditions

- Phase 2b manual smoke test fully signed off.
- Claude Code review session for Phase 2c has reported **PASS**.
- Working tree clean.
- API keys for LLM + embedder (same as Phase 2b — episodic layer wraps a retriever which uses an embedder).

## Setup

```bash
cd /Users/sam/projects/agent-foundry
ls projects/  # should now include the third example, e.g. memory_qa/
```

## Tests

### Test 1 — Working memory layer windowing

**What we're verifying**: an agent configured with `WorkingMemoryLayer(max_messages: 5)` running for 10 turns includes exactly the last 5 message turns in its prompt.

**Run**:

```bash
# Run an agent with multi-turn input; the third example project should
# support this. Trigger 10 turns (or however the example's flow works).
uv run python -m foundry run projects/<memory_example> --input '...turn1...'
# Inspect the prompt that went to the LLM on turn 10
jq '.prompt_messages | length' ~/.foundry/runs/<latest>/llm_calls.jsonl | tail -1
```

**Expected**: prompt on turn 10 contains the last 5 turns (plus system prompt). NOT 10, NOT 4.

**If it fails**:
- Off-by-one → fix session; window arithmetic.
- Layer not applied → coordinator not iterating layers; fix session.

- [ ] Pass

### Test 2 — Episodic memory wraps a retriever

**What we're verifying**: with `EpisodicMemoryLayer` bound to a seeded retriever, the agent's prompt includes top-K past snippets in the configured placement (e.g., `system_suffix`).

**Run**:

```bash
# The example project should seed a retriever with some past episodes
# (e.g., a small JSON of prior Q&As).
uv run python -m foundry run projects/<memory_example> --input '{"query": "..."}'

# Inspect:
jq 'select(.event_type == "memory.read")' ~/.foundry/runs/<latest>/events.jsonl
# Should show layers_read includes 'episodic' and the retrieved snippets
```

**Expected**:
- `memory.read` event lists `episodic` in `layers_read`.
- The LLM prompt (in `llm_calls.jsonl`) contains the retrieved snippets in the configured placement.

**If it fails**:
- No memory.read event → memory coordinator not running; fix.
- Snippets present but in wrong placement → prompt_assembly bug; fix.

- [ ] Pass

### Test 3 — Semantic memory consolidation

**What we're verifying**: with `SemanticMemoryLayer(consolidate_every_n: 3)`, after 3+ turns a consolidator prompt runs and writes synthesised content into the configured state field.

**Run**:

```bash
# Run for at least N turns (whatever consolidate_every_n is set to)
uv run python -m foundry run projects/<memory_example> --input '...'
jq 'select(.event_type == "memory.consolidate")' ~/.foundry/runs/<latest>/events.jsonl
# Inspect the consolidated content
jq '.state.<configured_field>' ~/.foundry/runs/<latest>/final_state.json
```

**Expected**:
- `memory.consolidate` event emitted with `input_tokens` + `output_tokens` populated.
- Configured state field contains synthesised content.

**If it fails**: consolidator never fires → cadence check broken; fix.

- [ ] Pass

### Test 4 — Memory degrades gracefully on retriever failure (default)

**What we're verifying**: with `failure_mode: degrade` (the default), a failed retriever in the episodic layer → empty contribution + warning event + run completes.

**Run**:

```bash
# Misconfigure the episodic layer's retriever (e.g., wrong embedder
# dim or non-existent vector store). Run the agent.
uv run python -m foundry run projects/<memory_example> --input '...'
jq 'select(.event_type == "memory.warning")' ~/.foundry/runs/<latest>/events.jsonl
echo "exit=$?"
git checkout -- projects/<memory_example>/
```

**Expected**:
- Exit code 0.
- Warning event names the failed layer.
- The agent's prompt does NOT contain episodic snippets (the layer contributed nothing).
- Final answer is produced (just without episodic context).

**If it fails**: run aborted → degrade mode is broken; fix session.

- [ ] Pass

### Test 5 — Memory fail-strict raises

**What we're verifying**: switching to `failure_mode: fail_strict` causes the same failure to abort the run with `MemoryLayerError`.

**Run**:

```bash
# Same misconfig as Test 4 but flip failure_mode: fail_strict in agent.yaml
uv run python -m foundry run projects/<memory_example> --input '...' ; echo "exit=$?"
git checkout -- projects/<memory_example>/
```

**Expected**: non-zero exit; `MemoryLayerError` raised; structured error names the layer and the cause.

**If it fails**: same error as degrade → mode switch not wired; fix session.

- [ ] Pass

### Test 6 — Envelope token cap truncates last layer first

**What we're verifying**: with `max_envelope_tokens` set low, the last-listed memory layer is truncated first and a `truncated: true` flag appears in the event.

**Run**:

```bash
# Set max_envelope_tokens to a value smaller than what the layers
# would normally contribute (e.g., 200 tokens). Run.
uv run python -m foundry run projects/<memory_example> --input '...'
jq 'select(.event_type == "memory.read") | .truncated' ~/.foundry/runs/<latest>/events.jsonl
git checkout -- projects/<memory_example>/
```

**Expected**: event has `truncated: true` (or similar) and identifies WHICH layer(s) got truncated — the last-listed first.

**If it fails**: truncation order wrong → fix to last-first per spec.

- [ ] Pass

### Test 7 — Layer-name uniqueness validator (adversarial)

**What we're verifying**: declaring two memory layers with the same name in `agent.yaml` raises `ConfigValidationError` at load.

**Run**:

```bash
# Edit agent.yaml: add a second layer with the same name as an existing
# layer
uv run python -m foundry run projects/<memory_example> --input '...' ; echo "exit=$?"
git checkout -- projects/<memory_example>/
```

**Expected**: non-zero at load; error names both duplicate names.

**If it fails**: missing validator; fix.

- [ ] Pass

### Test 8 — FunctionNode end-to-end in a sequential flow

**What we're verifying**: a `sequential` flow `[normalize_input_function, hello_agent, format_output_function]` runs in order; both Python functions execute; agent runs in between; final state reflects the full pipeline.

**Run**:

```bash
uv run python -m foundry run projects/<function_node_example> --input '...'
jq 'select(.event_type | startswith("function_node"))' ~/.foundry/runs/<latest>/events.jsonl
jq '.state' ~/.foundry/runs/<latest>/final_state.json
```

**Expected**:
- Two `function_node.completed` events (one per function).
- One `agent.completed` event.
- Final state shows the agent's output AND the post-format function's transformation.

**If it fails**: functions ran but agent didn't (or vice-versa) → compiler not threading nodes correctly.

- [ ] Pass

### Test 9 — FunctionNode state visibility drops out-of-scope writes

**What we're verifying**: a function declared with `read: [a, b], write: [c]` that returns `{a: ..., c: ...}` results in ONLY `c` being committed to state; `a` is dropped + a warning event emitted.

**Run**:

```bash
# The example project should include a FunctionNode designed to test
# this (or you can temporarily modify one to return an out-of-scope key).
uv run python -m foundry run projects/<function_node_example> --input '...'
jq 'select(.event_type == "function_node.warning")' ~/.foundry/runs/<latest>/events.jsonl
jq '.state' ~/.foundry/runs/<latest>/final_state.json
# Confirm `a` was NOT modified (matches its pre-function value)
```

**Expected**:
- Warning event names the dropped field(s).
- Final state's `a` is unchanged; `c` reflects the function's value.

**If it fails**: out-of-scope write committed → state visibility not enforced on functions; this is the same architectural property as agent state visibility — a P0 bug.

- [ ] Pass

### Test 10 — FunctionNode observability events

**What we're verifying**: `function_node.started` + `function_node.completed` events both fire with `node_name`, `node_version`, `fields_written`, `bytes_delta`, `latency_ms`.

**Run**:

```bash
uv run python -m foundry run projects/<function_node_example> --input '...'
jq 'select(.event_type | startswith("function_node"))' ~/.foundry/runs/<latest>/events.jsonl
```

**Expected**: events present with ALL the required attributes (non-null, non-zero where applicable).

**If it fails**: missing attributes → telemetry incomplete; fix.

- [ ] Pass

### Test 11 — Namespace collision (adversarial)

**What we're verifying**: defining an agent and a function with the same name → `CompileError` at load.

**Run**:

```bash
# Create a function with the same name as an existing agent in the
# project (or vice versa)
uv run python -m foundry run projects/<example> --input '...' ; echo "exit=$?"
git checkout -- projects/<example>/
```

**Expected**: non-zero exit at compile; `CompileError` names both colliding entries.

**If it fails**: collision allowed → fix.

- [ ] Pass

### Test 12 — Mixed-flow validation

**What we're verifying**: a `graph` flow's `from`/`to` references resolve to either agents or functions interchangeably; an unknown reference raises `CompileError`.

**Run** (positive case):

```bash
# The example project should include a graph flow with both agent and
# function nodes; run it.
uv run python -m foundry run projects/<example_with_graph_flow> --input '...'
```

**Expected**: runs successfully; both agents and functions appear in the run artifact's node-trace.

**Run** (negative case):

```bash
# Add a from/to reference pointing at a non-existent node
uv run python -m foundry run projects/<example> --input '...' ; echo "exit=$?"
git checkout -- projects/<example>/
```

**Expected**: non-zero at compile; `CompileError` names the dangling reference.

**If it fails**:
- Positive case fails → graph flow doesn't accept mixed kinds; fix.
- Negative case silently succeeds → compile-time validation missing; fix.

- [ ] Pass

### Test 13 — Phase 2 cumulative regression check

**What we're verifying**: closing out Phase 2 — the hero demos from 2a and 2b STILL work.

**Run**:

```bash
# 2a hero demo
uv run python -m foundry run projects/hello --input '{"name": "world"}'
# 2b hero demo
uv run python -m foundry run projects/<rag_example> --input '{"query": "..."}'
```

**Expected**: both run successfully; output unchanged from their respective sign-offs.

**If it fails**: regression — fix before declaring Phase 2 complete.

- [ ] Pass

### Test 14 — Schema final shape audit

**What we're verifying**: after Phase 2c, the cumulative schemas have all the fields they should.

**Run**:

```bash
# Should now contain ALL of these
grep -nE 'connections_required|cacheable|cache_ttl_s|cache_scope' src/foundry/config/schemas.py

# Should now contain ALL of these on AgentSpec
grep -nE 'tool_allowlist|state_scope|semantic_cache|retrievers|memory' src/foundry/config/schemas.py

# Should exist
ls src/foundry/core/{tool,connection,state,embedder,retrieval,cache,memory,node,function_node}.py
ls src/foundry/{cache,retrieval,memory,connections,auth,catalog}/
```

**Expected**: each grep returns matches for every field; each `ls` succeeds without errors.

**If it fails**: a 2a/2b deliverable got missed and 2c didn't catch it; loop back.

- [ ] Pass

### Test 15 — Commit hygiene

Same as prior phases.

- [ ] Pass

## Sign-off

- [ ] All 15 tests passed.
- [ ] Phase 2a and Phase 2b hero demos still work (regression-clean).
- [ ] Cumulative schemas contain every Phase 2 field.
- [ ] **Phase 2 is COMPLETE.**
- [ ] Ready to start Phase 3 (single-agent orchestration on LangGraph).

Signed off: ____________________ Date: __________

Add to `docs/_retros/phase_2c.md`: especially anything cumulative across 2a/2b/2c — e.g., "the sub-phase split caught X drift early" or "we'd cut the split differently next time".
