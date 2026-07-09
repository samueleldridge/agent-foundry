# Phase 3 handoff — single-agent orchestration on LangGraph

**Session date:** 2026-07-09
**Branch:** `main`
**Status:** Phase 3 implementation complete; awaiting AI review + operator
manual smoke test. No live API keys exist in the dev sandbox — every
live-path assertion below was verified against `httpx.MockTransport`
(the established Phase 1/2 pattern), including the kill+resume gate.

## Pre-work landed first (from the 2c review)

1. `test(retrieval)`: the hybrid-parallelism test replaced its wall-clock
   bound with a deterministic 2-party `asyncio.Barrier` rendezvous — the
   call completes only if both branches are in-flight simultaneously, so
   a sequential regression deadlocks (bounded by `wait_for`) instead of
   depending on machine load.
2. `feat(runtime)`: an agent configuring BOTH memory and semantic_cache
   (silent bypass, 2c deviation 4) now yields a `CompileWarning` —
   printed to stderr at compile and re-emitted as a `WarningEvent`
   (`cache.semantic.bypassed_by_memory`) right after `run.started`.
3. `docs(errata)`: docs/26 notes the `MemoryConfigError` (field/type/file)
   vs `CompileError` (scope/slot) split; both load-time.

## What this session built

1. **`foundry.orchestration.compiler` + `.patterns`** — compilation moved
   out of the adapter. `compile_project` (alias `compile_system`;
   `CompiledSystem = CompiledProject` until Phase 7 grows the multi-agent
   shape) lives in `compiler.py`; `patterns.plan_flow` executes `single`
   plus the 2c one-agent `sequential`, and raises structured "compiles in
   Phase 7" `CompileError`s for parallel / supervisor / graph /
   multi-agent sequential (references stay fully validated first, so a
   dangling ref is still reported as the ref, not the stub). No langgraph
   on this side of the boundary; the adapter re-exports `compile_project`
   so all Phase 1–2 call sites are unchanged.
2. **Graph-native agent step.** `execution.AgentStepRuntime` slices the
   agent step into node-sized methods — `begin` → `llm_round` ⇄
   `dispatch_tools` → `finish`, plus `start_turn`/`end_turn` around the
   loop for memory agents — and the adapter wires them as real StateGraph
   nodes with conditional edges. ALL conversation state (messages, tool
   results, turn/round counters, recent window, semantic-cache key) lives
   in a new checkpointed `conv` graph channel, so the 2c tool loop and
   memory turn loop now run THROUGH the graph and checkpoint at every
   node boundary. Sequential flows chain function nodes around the agent
   sub-graph exactly as before; `final_state.json`, event order and all
   Phase 1–2c artifact shapes are byte-compatible (the 403-test suite
   passed unmodified except one Phase-message assertion).
3. **`foundry.runtime.checkpointers`** — langgraph-FREE (stdlib sqlite3):
   the `--checkpoint` vocabulary, `~/.foundry/checkpoints/<project>.sqlite`
   path (FOUNDRY_HOME-aware), and `SqliteCheckpointStore` persisting the
   serde's opaque `(type, bytes)` rows. The langgraph-facing bridge lives
   in `_langgraph_types.py` (already an allowlisted importer):
   `FoundrySqliteSaver` keeps `InMemorySaver` semantics, mirrors every
   committed checkpoint/write/blob to SQLite and rehydrates on
   construction — a fresh process resumes exactly where a killed one
   stopped. `build_checkpointer("memory"|"sqlite"|"none")` selects.
4. **Resume.** Graph thread id = run id. `run_project` inspects
   `aget_state`: pending nodes on the last checkpoint → resume
   (`ainvoke(None)`, input ignored); no pending nodes → fresh invocation
   (a completed thread reruns fresh — documented semantics). The artifact
   dir is shared across processes: `RunArtifactWriter.next_sequence()`
   lets the resumed run continue the RunEvent sequence, `metadata.json`
   records `resumed` + `checkpointer`, `run.started` is emitted once per
   process (by design — the audit trail shows both attempts).
5. **Tracing.** `foundry.observability.tracing` (OTel API only):
   `foundry_span` / `set_span_attributes` / `worker_id` with attribute
   hygiene (None dropped, non-primitives → str, lazily-resolved tracer).
   Spans: `foundry.run` (run_id, project, system_version, pin_set_hash,
   started_at, worker_id, resumed; status/duration/token totals at end),
   `foundry.node` per graph node (run_id, project, node, agent),
   `foundry.llm` per provider call (run_id, project, agent, provider,
   model, tool_schemas_count; tokens/latency/cost/stop_reason post-call).
   All nodes parent under the run span — one trace per run.
6. **CLI.** `foundry run` gains `--stream` (every RunEvent printed to
   stdout as JSONL the moment it is emitted; typed output last),
   `--checkpoint memory|sqlite|none` (default **memory** — a checkpointer
   is always attached unless explicitly disabled), and `--run-id`
   (validated ULID; the resume surface). Exit codes unchanged
   (0/1/2; bad `--checkpoint` or `--run-id` → 2).

## Hero commands

```bash
export ANTHROPIC_API_KEY=...
# streaming
uv run python -m foundry run projects/hello --input '{"name": "world"}' --stream
# kill+resume (Ctrl-C mid-run, then rerun with the printed run id)
uv run python -m foundry run projects/hello --input '{"name": "world"}' \
  --checkpoint sqlite --run-id <RUN_ID>
sqlite3 ~/.foundry/checkpoints/hello.sqlite '.tables'   # inspect
```

## Deviations from the docs (all deliberate)

1. **`CompiledSystem` is an alias, not the docs/31 pydantic model.** The
   docs/31 `CompiledSystem` (BaseModel with `agents: dict[str, BaseAgent]`,
   `state_graph`, `run()`/`astream()`/`resume()` methods) presumes the
   multi-agent registry shape — Phase 7. Phase 3 keeps the Phase 2
   `CompiledProject` dataclass and aliases the name; `run_project` remains
   the execution entry point.
2. **Streaming is RunEvent-level, not provider-token-level.** `--stream`
   emits the docs/10 RunEvent stream incrementally (satisfies the exit
   gate); native per-provider SSE streaming (`LLMDelta` with real deltas)
   was NOT built — `ProviderAdapter.stream()` remains the Phase 1
   synthesized single-delta wrapper despite its docstring's "Phase 3"
   forecast. Backlog (v1.1 or Phase 8's API streaming work).
3. **No `langgraph-checkpoint-sqlite` dependency.** The official package
   would drag a new pin and put langgraph imports in a third module; the
   own-saver bridge (~90 lines over `InMemorySaver`) keeps the CLAUDE.md
   two-file import boundary intact. Contract test + ruff allowlists are
   unchanged.
4. **Serde stays permissive.** LangGraph's `JsonPlusSerializer` logs a
   one-time "unregistered type" warning per foundry pydantic type it
   deserializes from a checkpoint (FoundryMessage, ModelResponse, …). An
   explicit allowlist was rejected for Phase 3: user function nodes may
   put arbitrary pydantic values into state, and a miss would BLOCK their
   resume. Phase 9 hardening: `LANGGRAPH_STRICT_MSGPACK` + a foundry
   allowlist once state value types are locked.
5. **The 2c `turns` convention survives.** Checkpointing replaces its
   crash-fragility (kill mid-turn → resume, gate 2 covers it), but the
   real conversation surface (one process per user turn) needs the Phase
   8 API; `_TURNS_FIELD` remains the only in-run multi-turn driver.
6. **`foundry.node` span attributes chosen locally** (run_id, project,
   node, agent) — docs/01 § attribute spec enumerates `foundry.run` /
   `foundry.llm` / `foundry.tool` etc. but not `foundry.node`; the exit
   gate's run-id/system/agent triple is present on every span kind.
7. **Sub-node names are reserved.** The agent step's graph nodes are
   `<agent>__llm/tools/finish/turn/turn_end` (":" is reserved by
   LangGraph); a function node named like one raises a structured error.
8. **Completed-thread rerun = fresh run.** `--run-id` of a COMPLETED run
   re-executes (fresh input) on the same thread rather than replaying the
   stored output; only interrupted runs (pending nodes) resume. Test
   `test_rerun_of_a_completed_run_id_starts_fresh` pins it.
9. **`run.started` emits once per process** on a resumed run (sequence
   continues; `run.failed` from the killed process stays in the log).
   The alternative (suppressing it) would hide the resume from the audit
   trail.

## Interface notes for Phase 4

- Compile via `foundry.orchestration.compiler.compile_project` (the eval
  harness should NOT import the adapter for compilation); execute via
  `foundry.runtime.langgraph_adapter.run_project`. Library default is
  `checkpointer="none"` — evals need no checkpointer; the CLI default
  (`memory`) lives in `execute_run`.
- `run_project` params: `checkpointer`, `checkpoint_db`, `start_sequence`;
  `RunResult.resumed` reports what happened. `event_sink` receives every
  RunEvent synchronously — the eval harness can tee it for per-case traces.
- `AgentStepRuntime` is the reusable per-node engine; Phase 7's pattern
  compilers should instantiate one per agent and reuse
  `_wire_graph`-style sub-graph expansion rather than re-growing a
  monolithic node.
- Span assertions in tests: shared `span_exporter` fixture in
  `tests/conftest.py` (the OTel provider is process-global; install-once).

## Exit-gate confirmation

| Gate | How verified | Outcome |
|---|---|---|
| Single-agent system runs end-to-end through LangGraph with a checkpointer attached | every hello/rag_hello/memory_hello integration test now runs the node-decomposed StateGraph with the CLI-default in-memory checkpointer; `test_checkpoint_none_still_runs_end_to_end` covers opt-out | ✅ (mock) / ⏳ operator |
| Kill mid-run (raise after N tool calls), restart same run id → resumes and completes | `test_kill_after_tool_call_then_resume_completes` (tool executes → checkpoint → next LLM call 401s → exit 1; rerun same id: first request already carries the tool result, tool NOT re-executed, exit 0, `resumed: true`) + `test_memory_turn_loop_resumes_mid_conversation` (turn 1 not re-asked, window restored, single consolidation) + unit cross-instance saver test | ✅ (mock) / ⏳ operator |
| `foundry run --stream` emits incremental output | `test_stream_emits_runevents_as_jsonl_then_output` (ordered lifecycle JSONL, run_id-threaded, typed output last); emission is synchronous-per-event by construction | ✅ |
| Trace spans include run id, system name, agent name | `test_spans_include_run_id_system_and_agent` (`foundry.run`/`foundry.node`/`foundry.llm`; one trace, nodes parented under the run) + tracing unit tests | ✅ |
| Adapter module(s) are the only langgraph importers (lint + contract) | ruff banned-api unchanged; `test_no_banned_imports_outside_allowlisted_files` still pins `langgraph_adapter.py` + `_langgraph_types.py` only; new `checkpointers.py` is stdlib-only | ✅ |

## Definition-of-done confirmation

- `uv run ruff check src/ tests/` — zero violations.
- `uv run mypy --strict src/foundry/` — passes (156 files).
- `uv run pytest tests/` — 429 passed (the prior 403 intact +1 pre-work
  +25 Phase 3; one 2c assertion updated: the graph-flow stub message now
  names Phase 7).
- All three example projects run through the new graph path (their
  Phase 1/2 integration suites unmodified).
- `run_id` threaded through spans, checkpoint thread ids, resumed event
  sequences and artifacts; no secrets in code/configs/fixtures (resume
  suite reuses the fake-key hygiene pattern).
- Scope check: no multi-agent pattern execution, no HITL/interrupt, no
  eval harness, no API layer, no OTel exporter/metrics wiring.

**Phase 3 is COMPLETE pending review + operator smoke test. Next session
starts Phase 4 (eval harness) fresh.**
