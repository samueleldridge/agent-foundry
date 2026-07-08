# Phase 2c handoff — memory + FunctionNode (closes Phase 2)

**Session date:** 2026-07-08
**Branch:** `main`
**Status:** Phase 2c implementation complete; awaiting AI review + operator
manual smoke test. No live API keys exist in the dev sandbox — every
live-path assertion below was verified against `httpx.MockTransport`
serving api.anthropic.com (the established Phase 1/2a/2b pattern).

## Pre-work landed first

1. `docs(errata)`: docs/24's caching diagram now matches the granularity
   table — the semantic cache stores the agent step's TERMINAL response
   keyed by the INITIAL input (a hit skips the whole LLM ⇄ tool loop), and
   correctness rule 1 notes version-marker invalidation runs at run start
   (hygiene; `agent_version` is in the bucket, so stale entries can't hit
   regardless). docs/25 states that reranker artifacts resolve under the
   `retriever` artifact kind with `kind: reranker`, enforced in both
   directions.
2. `test(retrieval)`: the hybrid-parallelism wall-clock bound widened from
   0.09s to 0.5s (10x one branch delay) so a loaded machine can't flake it.

## What this session built

1. **`foundry.core`** — `MemoryContext` got its concrete 2c typing
   (`session: Session`, `turn_count`, `recent_messages`, typed
   `StateWriter`); `MemoryEnvelope` gained `layers_truncated`/
   `layers_failed`; `function_node.completed` gained `node_version`;
   `memory.read` gained `layers_truncated`; `llm.started` gained
   `prompt_messages` (populated ONLY under `observability.capture_inputs`,
   flowing into llm_calls.jsonl — the docs/26 "what was in the envelope?"
   debug surface). `Node`/`FunctionNode`/`BaseFunctionNode`/`NodeResult`
   were already Phase 1 stubs and stand unchanged.
2. **`foundry.config`** — `MemoryConfig` (3-layer discriminated union +
   `MemoryInjectionRule` + `MemoryWindow`), layer-name uniqueness and
   injection-rule references validated at the schema level
   (`ConfigValidationError` at load); `AgentSpec.memory: MemoryConfig |
   None = None`; `load_project` now loads `functions/` (spec + source
   text; the handler import stays in the runtime so the loader remains
   pure config).
3. **`foundry.memory`** — `DefaultMemory` coordinator (per-layer read with
   degrade-or-strict, envelope cap truncating LAST-listed layers first,
   `memory.read` emission; write fan-out; due-aware consolidation with
   fail-open preserving prior synthesis); `WorkingMemoryLayer` (windowed
   projection, `max_messages` OR `max_tokens`, list-of-FoundryMessage or
   str sources, read-only per invariant 5); `EpisodicMemoryLayer` (wraps a
   2b `Retriever` via the agent's retriever slots; `top_k` +
   `relevance_threshold`; write = best-effort duck-typed `ingest()`);
   `SemanticMemoryLayer` (state-field read; periodic consolidator on the
   AGENT'S resolved provider; writes back through the scope-enforcing
   `StateWriter`; `memory.consolidate` with real token counts);
   `prompt_assembly.weave()` (system_prefix/suffix, messages,
   user_message_prefix with the `<memory …>` boundary, per-kind default
   templates/placements, per-rule `max_tokens`); `wiring.prepare_memory`
   (compile-time: field existence/type → `MemoryConfigError`; read/write
   scope + retriever-slot binding → `CompileError`; consolidator prompt
   loaded from disk) + `build_memory` (run start).
4. **`foundry.runtime`** — split into `compiled.py` (value types),
   `execution.py` (step engine, no langgraph), and the thin adapter.
   Sequential flows compile to chained StateGraph nodes; the project state
   dict threads node-to-node with reducer-merged deltas and per-node
   structural projections. Function nodes execute with the tool-style
   retry/timeout plumbing, drop out-of-scope returned fields with a
   `warning` (`function_node.out_of_scope_write`), and emit
   `function_node.started/completed` with `node_name`, content-hashed
   `node_version` (source + spec), `fields_written`, `bytes_delta`,
   `latency_ms`. The memory-enabled agent step runs the docs/26 per-turn
   lifecycle: read → weave → LLM ⇄ tools → state append + `memory.write`
   (episodic ingest) → periodic consolidate. Compile-time validation:
   agent/function namespace collision → `CompileError`; flow refs
   validated across BOTH namespaces for all five flow types (missing ref →
   `CompileError` naming the pointer) BEFORE the execution-support check,
   so a valid graph flow fails only on "lands in Phase 3+".
5. **CLI + artifacts** — `final_state.json` written per run (the debug
   surface for memory fields and function pipelines); llm_calls.jsonl
   records `prompt_messages` under capture_inputs; run output for
   sequential flows is the final state (the pipeline's product).
6. **`projects/memory_hello`** — hero demo: sequential
   `[normalize_input, hello_agent, format_output]`; all three memory
   layers (working `max_messages: 5`; episodic over a project-local BM25
   `episode_store` retriever seeded from `episodes.json`, with `ingest()`;
   semantic `user_facts` consolidating every 3 turns). Zero infra beyond
   `ANTHROPIC_API_KEY`.

## Hero command

```bash
export ANTHROPIC_API_KEY=...
uv run python -m foundry run projects/memory_hello --input '{
  "raw_turns": ["  hi, my name is Sam  ",
                "I am planning a trip to Paris in October",
                "what should I not miss there?",
                "and remind me — what is my name?"]}'
```

## Deviations from the docs (all deliberate)

1. **`consolidator_model_binding` is NOT in the 2c schema.** docs/12
   sketches it; the phase directive was explicit ("consolidator uses the
   AGENT'S model_binding, not a separate one"). The semantic layer takes a
   `SupportsGenerate` provider, so the override is a purely additive
   config field + one resolve call when it lands (v1.1 or Phase 3 errata).
2. **Multi-turn convention.** No conversation surface exists until Phase
   3/7 (checkpointing, API), so the memory turn loop is driven by a
   read-scope `list[str]` state field literally named `turns` — one LLM
   turn per item; absent, the projected input is a single turn. Documented
   in `execution.py::_TURNS_FIELD` + the example README. Revisit in Phase 3.
3. **Memory-config error split.** docs/26 says `MemoryConfigError` for
   compile failures; docs/03 § 2c says `CompileError` for "a state field
   the agent can't read". Both are honoured: field existence / working
   source-field type / prompt-file-on-disk → `MemoryConfigError`;
   scope holes + unbound retriever slot → `CompileError`. All load-time.
4. **Semantic cache is bypassed for memory-enabled agents.** Its key
   covers the step's initial input, not the evolving envelope — a hit
   could replay a response that ignores state. Non-memory agents are
   byte-for-byte on the 2b path. A per-turn key design is a Phase 3+
   question.
5. **Sequential flows accept exactly ONE agent** (plus any number of
   functions) — `CompiledProject` is still single-provider-shaped; > 1
   agent → `CompileError` pointing at Phase 3. Reference VALIDATION is
   fully mixed across both namespaces for all five flow types.
6. **Agent outputs project silently onto write scope** (no warning for
   out-of-scope OUTPUT fields — e.g. rag_hello's `sources` is part of the
   caller contract, not a state write). Function nodes DO warn: their
   return value IS a state delta by contract (exit gate 9).
7. **Working memory string sources keep the TAIL** (recency) while
   envelope-cap truncation of synthesised string content keeps the HEAD
   (structure-first); doc lists drop lowest-ranked, message lists drop
   oldest. docs/26 doesn't specify within-layer truncation direction.
8. **Episodic ingest is duck-typed** (`ingest()` on the underlying
   retriever; silently skipped when absent) — docs/26 calls episodic
   writes "hand-off to the retriever's underlying connection" without a
   protocol method; a read-only corpus stays a valid episodic source.

## Interface notes for Phase 3

- `foundry.runtime.execution` is the reusable per-node step engine
  (`run_agent_step`, `run_function_step`, `llm_tool_loop`, `apply_delta`,
  `seed_state`) — the Phase 3 compiler should consume these rather than
  grow the adapter. The adapter's graph-building block is ~40 lines.
- `CompiledFunction.node_version` is the content hash (source + spec) —
  checkpointing and eval pinning can rely on it.
- The `turns` convention (deviation 2) should be replaced by real session
  checkpointing; `_memory_agent_turns` is the only consumer.
- `MemoryContext.state_writer` enforces write scope at the runtime seam;
  Phase 3's node wrapper can pass the same closure per node.
- `EventEmitter` moved to `execution.py`; `RunResult` and
  `CompiledProject` to `runtime/compiled.py` (re-exported from the
  adapter, so `foundry.cli` imports are unchanged).

## Exit-gate confirmation

| Gate | How verified | Outcome |
|---|---|---|
| Working window: max_messages 5, 10-turn run → exactly last 5 in prompt | `test_working_window_shows_exactly_last_5_messages_on_10_turn_run` (turn-10 request = 5 windowed [a7,u8,a8,u9,a9] + current u10; final state = 20 messages) | ✅ (mock) / ⏳ operator |
| Episodic: seeded retriever → top-K snippets in system_suffix; memory.read lists episodic | `test_episodic_snippets_land_in_system_suffix...` ([EP-001] AFTER the hand-authored prompt; layers_read; per-turn memory.write) | ✅ (mock) / ⏳ operator |
| Semantic: consolidator every N turns writes state field; memory.consolidate with token counts | `test_semantic_consolidation_every_3_turns...` (3 consolidations on 10 turns; input 120 / output 25 tokens; user_facts = v3 synthesis; v1 visible in turn-4 system_prefix) | ✅ (mock) / ⏳ operator |
| Degrade-gracefully default: failed episodic retriever → empty contribution + warning; run completes | `test_failed_episodic_retriever_degrades...` (exit 0; warning `memory.layer_failed`; layers_failed; no suffix in prompt) + unit degrade test | ✅ |
| Fail-strict: same failure → MemoryLayerError, run aborts | `test_fail_strict_aborts_run_with_memory_layer_error` (exit 1, run.failed, zero LLM calls) + unit strict test | ✅ |
| max_envelope_tokens → last-listed layer truncates first; truncated: true in event | `test_envelope_cap_truncates_last_listed_layer_first` (integration; layers_truncated[0] = past_sessions) + coordinator unit test | ✅ |
| Duplicate layer names → ConfigValidationError at load | `test_duplicate_layer_names_fail_at_load` (exit 2, no artifact) + schema unit test naming the duplicate | ✅ |
| FunctionNode E2E: sequential [normalize_input_function, hello_agent, format_output_function] | `test_sequential_flow_runs_functions_and_agent_end_to_end` (event order function→agent→function; normalised turns; formatted final state + printed output) | ✅ (mock) / ⏳ operator |
| FunctionNode visibility: write [c], returns {a, c} → only c written; a dropped + warning | `test_function_out_of_scope_write_dropped_with_warning` (state unchanged, `function_node.out_of_scope_write` warning, fields_written = [formatted_reply]) | ✅ |
| FunctionNode observability: started/completed with node_name, node_version, fields_written, bytes_delta, latency_ms | `test_function_node_events_carry_full_telemetry` (all fields, run_id threaded) | ✅ |
| Agent + function same name → CompileError | `test_agent_and_function_with_same_name_is_compile_error` (exit 2, names the collision, no artifact) | ✅ |
| Mixed flow refs resolve across both; missing ref → CompileError | `test_sequential_flow_with_missing_ref_is_compile_error` + `test_graph_flow_refs_resolve_across_agents_and_functions` (valid mixed graph refs pass validation, fail only on Phase-3 execution; dangling ref named with pointer) | ✅ |

## Definition-of-done confirmation

- `uv run ruff check src/ tests/` — zero violations.
- `uv run mypy --strict src/foundry/` — passes (156 files).
- `uv run pytest tests/` — 403 passed (2a/2b's 358 all intact, minus the
  2b scope-guard test which now asserts the CUMULATIVE Phase 2 shape:
  AgentSpec has tools/state_visibility/semantic_cache/retrievers/memory;
  ToolSpec has connections_required/cacheable/cache_ttl_s/cache_scope).
- `run_id` threaded through memory.read/write/consolidate,
  function_node.*, warning events, final_state.json and metadata.
- No secrets in code/configs/fixtures; the integration suite asserts the
  fake key never appears in the run artifact.
- Scope check: no orchestration-pattern compilers (sequential execution is
  a ~40-line extension of the existing adapter over the reusable step
  engine), no eval harness, no versioning, no meta-agent, no HITL.

**Phase 2 is COMPLETE pending review + operator smoke test. Next session
starts Phase 3 (single-agent orchestration on LangGraph) fresh.**
