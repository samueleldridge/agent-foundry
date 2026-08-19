# Phase 2c — Manual Smoke Tests

**Phase scope**: `foundry.core.node` + `foundry.core.function_node` + `foundry.core.memory` + memory + FunctionNode config schemas + `foundry.memory` (DefaultMemory + 3 layers + prompt assembly) + remaining compile-time validation (namespace collision, mixed-flow refs) + a third example project demonstrating memory layers + a FunctionNode in a sequential flow. **Completing this sub-phase closes out Phase 2.**

**Reference**: [docs/03-development-phases.md § Phase 2c](../03-development-phases.md) exit gate; [docs/_phase_handoffs/phase_2c.md](../_phase_handoffs/phase_2c.md) for implementation notes (the third example is `projects/memory_hello`; multi-turn input arrives via the `raw_turns` input field → `normalize_input` → the agent's `turns` field, one LLM turn per item).

## Preconditions

- Phase 2b manual smoke test fully signed off.
- Claude Code review session for Phase 2c has reported **PASS**.
- Working tree clean.
- `ANTHROPIC_API_KEY` set (memory_hello needs no other API — the episodic
  retriever is in-process BM25, no embedder).

## Setup

```bash
cd <repo-root>
ls projects/   # hello  memory_hello  rag_hello
export ANTHROPIC_API_KEY=...
# Convenience: latest run dir
runs() { ls -d ~/.foundry/runs/* | tail -1; }
```

Ten-turn input used repeatedly below:

```bash
TURNS='{"raw_turns": ["hi, my name is Sam", "I am planning a trip to Paris in October", "what should I not miss?", "any good jazz clubs?", "and coffee near the river?", "how many days should I stay?", "what about day trips?", "is October rainy there?", "what should I pack?", "remind me, what is my name?"]}'
```

## Tests

### Test 1 — Working memory layer windowing

**What we're verifying**: `WorkingMemoryLayer(max_messages: 5)` over a 10-turn run puts exactly the last 5 conversation messages in the final turn's prompt.

**Run**:

```bash
uv run python -m foundry run projects/memory_hello --input "$TURNS"
# Inspect the prompt of the LAST llm call (capture_inputs is on by default)
jq -s '.[-1].prompt_messages | length' $(runs)/llm_calls.jsonl
jq -s '[.[-1].prompt_messages[].role]' $(runs)/llm_calls.jsonl
```

**Expected**: `6` — exactly 5 windowed messages (`assistant,user,assistant,user,assistant`) + the current user turn. NOT 19, NOT 5. (The last agent reply also correctly answers "what is my name?" with Sam — the name is OUTSIDE the window but inside the consolidated `user_facts`.)

**If it fails**:
- Off-by-one → window arithmetic in `WorkingMemoryLayer`; fix session.
- Layer not applied → coordinator not iterating layers; fix session.

- [ ] Pass

### Test 2 — Episodic memory wraps a retriever

**What we're verifying**: the episodic layer retrieves seeded past-session snippets from `episodes.json` into the configured `system_suffix` placement.

**Run**:

```bash
uv run python -m foundry run projects/memory_hello --input '{"raw_turns": ["planning a Paris trip"]}'
jq 'select(.event == "memory.read")' $(runs)/events.jsonl
jq -s '.[0].prompt_messages[0].content[0].text' $(runs)/llm_calls.jsonl | grep -o "Relevant past context"
```

**Expected**:
- `memory.read` lists `past_sessions` in `layers_read`, `layers_failed: []`.
- The system message contains `Relevant past context:` followed by `[EP-001]` (the seeded Paris episode), AFTER the hand-authored prompt text.
- `memory.write` events show per-turn ingestion into the episode store.

**If it fails**:
- No memory.read event → coordinator not running; fix.
- Snippets present but in wrong placement → prompt_assembly bug; fix.

- [ ] Pass

### Test 3 — Semantic memory consolidation

**What we're verifying**: `consolidate_every_n_turns: 3` → on a 10-turn run the consolidator fires at turns 3, 6, 9 and writes synthesised content to `user_facts`.

**Run**:

```bash
uv run python -m foundry run projects/memory_hello --input "$TURNS"
jq 'select(.event == "memory.consolidate")' $(runs)/events.jsonl
jq '.state.user_facts' $(runs)/final_state.json
```

**Expected**:
- Exactly 3 `memory.consolidate` events, `trigger: periodic`, with non-zero `input_tokens_summarised` + `output_tokens_written` + `latency_ms`.
- `user_facts` in final_state.json holds synthesised Markdown mentioning Sam / Paris.
- From turn 4 on, the system prompt STARTS with the "What you have learned about this user" block.

**If it fails**: consolidator never fires → cadence check broken; fix.

- [ ] Pass

### Test 4 — Memory degrades gracefully on retriever failure (default)

**What we're verifying**: with `fail_strict: false` (the project default), a failing episodic retriever → empty contribution + warning event + run completes.

**Run**:

```bash
sed -i '' 's/corpus_path: episodes.json/corpus_path: missing.json/' projects/memory_hello/agents/hello_agent/agent.yaml
uv run python -m foundry run projects/memory_hello --input '{"raw_turns": ["hello"]}' ; echo "exit=$?"
jq 'select(.event == "warning" and .category == "memory.layer_failed")' $(runs)/events.jsonl
git checkout -- projects/memory_hello/
```

**Expected**:
- Exit code 0; a formatted reply is still produced.
- Warning event names `past_sessions`; `memory.read` has `layers_failed: ["past_sessions"]`.
- The prompt contains NO `Relevant past context` block.

**If it fails**: run aborted → degrade mode is broken; fix session.

- [ ] Pass

### Test 5 — Memory fail-strict raises

**What we're verifying**: flipping `fail_strict: true` makes the same failure abort the run with `MemoryLayerError`.

**Run**:

```bash
sed -i '' 's/corpus_path: episodes.json/corpus_path: missing.json/' projects/memory_hello/agents/hello_agent/agent.yaml
sed -i '' 's/fail_strict: false/fail_strict: true/' projects/memory_hello/agents/hello_agent/agent.yaml
uv run python -m foundry run projects/memory_hello --input '{"raw_turns": ["hello"]}' ; echo "exit=$?"
git checkout -- projects/memory_hello/
```

**Expected**: exit 1; `MemoryLayerError` on stderr naming the layer (`past_sessions`) and the cause; `run.failed` in events.jsonl; NO llm calls were made.

**If it fails**: same behaviour as degrade → mode switch not wired; fix session.

- [ ] Pass

### Test 6 — Envelope token cap truncates last layer first

**What we're verifying**: a small `max_envelope_tokens` truncates the LAST-listed layer (`past_sessions`, the episodic layer) first; the event flags it.

**Run**:

```bash
sed -i '' 's/max_envelope_tokens: 4000/max_envelope_tokens: 100/' projects/memory_hello/agents/hello_agent/agent.yaml
uv run python -m foundry run projects/memory_hello --input '{"raw_turns": ["tell me about the Paris trip plan in detail", "more Paris trip planning details please"]}'
jq 'select(.event == "memory.read") | {truncated, layers_truncated, total_tokens_estimate}' $(runs)/events.jsonl
git checkout -- projects/memory_hello/
```

**Expected**: at least one `memory.read` with `truncated: true`, `layers_truncated` starting with `past_sessions`, and `total_tokens_estimate <= 100`.

**If it fails**: truncation order wrong → fix to last-first per docs/26 § Prompt assembly rule 6.

- [ ] Pass

### Test 7 — Layer-name uniqueness validator (adversarial)

**What we're verifying**: two memory layers with the same name → `ConfigValidationError` at load.

**Run**:

```bash
sed -i '' 's/name: past_sessions/name: short_term/' projects/memory_hello/agents/hello_agent/agent.yaml
uv run python -m foundry run projects/memory_hello --input '{"raw_turns": ["x"]}' ; echo "exit=$?"
git checkout -- projects/memory_hello/
```

**Expected**: exit 2; `ConfigValidationError` naming the duplicated name (`short_term`) and "unique"; no run artifact created.

**If it fails**: missing validator; fix.

- [ ] Pass

### Test 8 — FunctionNode end-to-end in a sequential flow

**What we're verifying**: `[normalize_input, hello_agent, format_output]` runs in order; both Python functions execute; the agent runs in between; final state reflects the full pipeline.

**Run**:

```bash
uv run python -m foundry run projects/memory_hello --input '{"raw_turns": ["  hi there  ", ""]}'
jq 'select(.event | startswith("function_node")) | {event, node_name, fields_written}' $(runs)/events.jsonl
jq '.state | {turns, reply, formatted_reply}' $(runs)/final_state.json
```

**Expected**:
- Two `function_node.completed` events (`normalize_input` then `format_output`) around ONE `agent.completed`.
- `turns` is normalised (`["hi there"]` — trimmed, empty dropped).
- `formatted_reply` = `[memory_hello] ` + the agent's `reply`.

**If it fails**: functions ran but agent didn't (or vice-versa) → the sequential graph isn't threading nodes correctly.

- [ ] Pass

### Test 9 — FunctionNode state visibility drops out-of-scope writes

**What we're verifying**: a function returning a field outside its `write` scope has that field DROPPED + a warning event; the in-scope field commits.

**Run**:

```bash
# Make format_output (write: [formatted_reply]) also try to overwrite `reply`
sed -i '' 's/return {"formatted_reply": f"\[memory_hello\] {reply}"}/return {"reply": "HACKED", "formatted_reply": f"[memory_hello] {reply}"}/' projects/memory_hello/functions/format_output/function.py
uv run python -m foundry run projects/memory_hello --input '{"raw_turns": ["hello"]}'
jq 'select(.event == "warning" and .category == "function_node.out_of_scope_write")' $(runs)/events.jsonl
jq '.state.reply' $(runs)/final_state.json    # must NOT be "HACKED"
git checkout -- projects/memory_hello/
```

**Expected**:
- Warning event names the dropped field (`reply`) and the node.
- Final state's `reply` is the AGENT's reply, untouched; `formatted_reply` committed.
- `function_node.completed.fields_written` lists only `formatted_reply`.

**If it fails**: out-of-scope write committed → state visibility not enforced on functions; this is the same architectural property as agent state visibility — a P0 bug.

- [ ] Pass

### Test 10 — FunctionNode observability events

**What we're verifying**: `function_node.started` + `function_node.completed` carry `node_name`, `node_version`, `fields_written`, `bytes_delta`, `latency_ms`.

**Run**:

```bash
uv run python -m foundry run projects/memory_hello --input '{"raw_turns": ["hello"]}'
jq 'select(.event | startswith("function_node"))' $(runs)/events.jsonl
```

**Expected**: both events per function, with `node_version` a non-empty content hash, `fields_written` non-empty, `bytes_delta > 0`, `latency_ms >= 0`, and the run's `run_id` on every event.

**If it fails**: missing attributes → telemetry incomplete; fix.

- [ ] Pass

### Test 11 — Namespace collision (adversarial)

**What we're verifying**: an agent and a function with the same name → `CompileError` at load.

**Run**:

```bash
cp -r projects/memory_hello/functions/normalize_input projects/memory_hello/functions/hello_agent
sed -i '' 's/name: normalize_input/name: hello_agent/' projects/memory_hello/functions/hello_agent/function.yaml
sed -i '' 's/functions: \[normalize_input, format_output\]/functions: [normalize_input, format_output, hello_agent]/' projects/memory_hello/system.yaml
uv run python -m foundry run projects/memory_hello --input '{"raw_turns": ["x"]}' ; echo "exit=$?"
rm -rf projects/memory_hello/functions/hello_agent && git checkout -- projects/memory_hello/
```

**Expected**: exit 2; `CompileError` naming `hello_agent` as a namespace collision; no run artifact.

**If it fails**: collision allowed → fix.

- [ ] Pass

### Test 12 — Mixed-flow validation

**What we're verifying**: flow `from`/`to`/step references resolve to agents AND functions interchangeably; an unknown reference raises `CompileError`. (Graph-flow EXECUTION is Phase 3 — here we verify its references validate at compile.)

**Run** (positive case — the sequential mixed flow IS the positive case, covered by Test 8; graph refs validate too):

```bash
python - <<'PY'
from pathlib import Path
p = Path("projects/memory_hello/system.yaml")
p.write_text(p.read_text().replace(
    "flow:\n  type: sequential\n  steps: [normalize_input, hello_agent, format_output]",
    "flow:\n  type: graph\n  start: normalize_input\n  edges:\n    - {from: normalize_input, to: hello_agent}\n    - {from: hello_agent, to: format_output}"))
PY
uv run python -m foundry run projects/memory_hello --input '{"raw_turns": ["x"]}' ; echo "exit=$?"
```

**Expected**: exit 2, but the `CompileError` complains ONLY about graph execution landing in Phase 3+ — NOT about any unknown node (the mixed agent+function refs resolved).

**Run** (negative case):

```bash
sed -i '' 's/to: format_output/to: ghost_node/' projects/memory_hello/system.yaml
uv run python -m foundry run projects/memory_hello --input '{"raw_turns": ["x"]}' ; echo "exit=$?"
git checkout -- projects/memory_hello/
```

**Expected**: exit 2; `CompileError` naming `ghost_node` and the `/flow/edges/…/to` pointer.

**If it fails**:
- Positive case complains about unknown nodes → mixed-kind resolution broken; fix.
- Negative case doesn't name the dangling ref → compile-time validation missing; fix.

- [ ] Pass

### Test 13 — Phase 2 cumulative regression check

**What we're verifying**: closing out Phase 2 — the hero demos from 2a and 2b STILL work.

**Run**:

```bash
# 2a hero demo
uv run python -m foundry run projects/hello --input '{"name": "world"}'
# 2b hero demo (needs VOYAGE_API_KEY + COHERE_API_KEY too)
uv run python -m foundry run projects/rag_hello --input '{"query": "what is the capital of France?"}'
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
# (note: the allowlist field is `tools:`; the scope field is `state_visibility:` — both present since 2a)
grep -nE '^    tools:|^    state_visibility:|^    memory:' src/foundry/config/schemas.py

# Should exist
ls src/foundry/core/{tool,connection,state,embedder,retrieval,cache,memory,node,function_node}.py
ls src/foundry/{cache,retrieval,memory,connections,auth,catalog}/
```

**Expected**: every field present; each `ls` succeeds without errors.

**If it fails**: a 2a/2b deliverable got missed and 2c didn't catch it; loop back.

- [ ] Pass

### Test 15 — Commit hygiene

Same as prior phases: conventional commits, no `Co-Authored-By: Claude`, no institution names, atomic per logical chunk.

```bash
git log --oneline -12
git log -12 --format=%B | grep -i "co-authored" ; echo "exit=$? (want 1)"
```

- [ ] Pass

## Sign-off

- [ ] All 15 tests passed.
- [ ] Phase 2a and Phase 2b hero demos still work (regression-clean).
- [ ] Cumulative schemas contain every Phase 2 field.
- [ ] **Phase 2 is COMPLETE.**
- [ ] Ready to start Phase 3 (single-agent orchestration on LangGraph).

Signed off: ____________________ Date: __________

Add to `docs/_retros/phase_2c.md`: especially anything cumulative across 2a/2b/2c — e.g., "the sub-phase split caught X drift early" or "we'd cut the split differently next time".
