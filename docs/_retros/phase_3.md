# Phase 3 retro

**What took longer than expected.** Making the tool loop and memory turn
loop run THROUGH the graph, not just inside a node. The naive reading of
the deliverable — wrap the 2c `run_agent_step` in one StateGraph node and
bolt on a checkpointer — would have passed a shallow version of the
kill+resume gate while re-executing every tool call on resume. Doing it
properly meant dissolving `llm_tool_loop` and `_memory_agent_turns` into
node-sized slices (`begin` / `llm_round` / `dispatch_tools` /
`start_turn` / `end_turn` / `finish`) with ALL conversation state moved
into a checkpointed `conv` channel. The 2c handoff's advice ("consume the
step engine, don't grow the adapter") was half-right: the step engine was
reusable, but its two loop functions had to be broken apart anyway —
loops that must checkpoint cannot stay Python loops.

**What changed from the plan.** (1) The SQLite checkpointer was expected
to be `langgraph-checkpoint-sqlite`; instead it's a ~90-line
`InMemorySaver` subclass over a stdlib sqlite3 store, because the
official package would add a pin AND a third langgraph-importing module —
the CLAUDE.md two-file boundary won. (2) Streaming shrank to
RunEvent-level JSONL; native provider SSE (real `LLMDelta`s) was forecast
by a Phase 1 docstring but is not in the docs/03 deliverable list, so it
went to the backlog rather than into scope. (3) `CompiledSystem` stayed
an alias of `CompiledProject` — the docs/31 pydantic shape presumes the
Phase 7 multi-agent registry.

**What was cheaper than expected.** Resume itself. Once state lived in
graph channels, LangGraph's own semantics did the work: thread id =
run id, `aget_state(...).next` distinguishes interrupted from completed,
`ainvoke(None)` continues. The whole CLI resume surface is ~15 lines.
Serde also just worked — pydantic FoundryMessages/ModelResponses
round-tripped through msgpack on the first try (with a permissive-mode
warning noted for Phase 9 hardening). And the three example projects
passed their unmodified Phase 1/2 suites on the first full run after the
graph rewrite — the artifact/event contracts held.

**Friction worth flagging.** (a) LangGraph reserves ":" in node names,
discovered by test failure; sub-nodes are now `<agent>__llm` etc. with a
collision guard. (b) The OTel tracer provider is process-global and
set-once, which forced a shared `tests/conftest.py` exporter fixture —
worth remembering for Phase 9's exporter work. (c) docs/01 enumerates
attributes for `foundry.run`/`foundry.llm` but not `foundry.node`; picked
(run_id, project, node, agent) locally — the spec table should gain a row.
(d) Verifying intermediate commits with `git stash push --keep-index` +
full test runs kept the eight-commit sequence honest at ~2s per gate;
cheap insurance, would repeat.

**Framework note (per the "don't switch mid-phase" rule).** No temptation
this phase — this was the first phase where LangGraph earned its pin:
checkpointing, pending-write replay and state snapshots came for free
once the graph shape was right. The cost paid in 2c (keeping the adapter
thin) was repaid here.
