# Phase 2c retro

**What took longer than expected.** The multi-turn question. Every memory
exit gate presumes "a 10-turn run", but nothing before Phase 3 defines how
turns ARRIVE — no checkpointed sessions, no API, one CLI invocation per
run. The layers themselves were quick (docs/26 is the tightest spec in the
set: lifecycle pseudocode, failure table, defaults table, unit list); the
half-day question was the driver. The answer — a read-scope `turns:
list[str]` state field consumed one-LLM-turn-per-item inside the agent
step — is honest to docs/26's per-turn lifecycle and lets a FunctionNode
normalise the turns first, but it is a Phase-2c convention, clearly marked
for replacement by Phase 3 checkpointing.

**What changed from the plan.** (1) The runtime split three ways
(`compiled.py` / `execution.py` / adapter) — the memory turn loop and
function-node steps don't need langgraph, and Phase 3's compiler will want
the step engine without the adapter; the import boundary forced a good
architecture. (2) `consolidator_model_binding` was deliberately dropped
from the schema (phase directive: the agent's own binding), against
docs/12's sketch — additive to restore. (3) Semantic cache and memory
don't compose yet: the cache key covers initial input, not the envelope,
so memory-enabled agents bypass it. (4) The docs/26-vs-docs/03 error-class
tension (MemoryConfigError vs CompileError) resolved as a split by
failure kind rather than picking a side.

**What was cheaper than expected.** Everything downstream of 2a/2b
investments. Episodic memory is ~60 lines because it's just a 2b
`Retriever` behind the slot accessor; the exit gates' degrade/strict pair
fell out of the coordinator's one try/except because retrievers already
raise structured `RetrievalError`s; the seeded corpus is the same BM25
in-process pattern as rag_hello with an `ingest()` method added. The
whole integration suite (14 tests incl. all 13 exit-gate rows) passed on
the FIRST run after the smoke script — the fresh-session-per-phase gates
have kept the contracts tight enough that composition just works.

**Friction worth flagging.** (a) `FunctionNodeCompleted` lacked
`node_version` in the Phase 1 event catalogue although the 2c exit gate
demands it — the "define the full union on day one" strategy still needs
per-phase field audits. (b) LangGraph's node protocol matches the state
parameter BY NAME (`state`), which cost one confusing mypy failure after
renaming a closure parameter. (c) The manual checklist predicted a
runnable graph-flow positive case; execution is Phase 3, so the checklist
was realigned to compile-level validation — checklists written before the
phase should mark execution-vs-validation assumptions.

**Cumulative 2a/2b/2c note for the sub-phase split.** The split earned its
keep: each sub-phase consumed the previous one's seams exactly as the
handoffs advertised (2a's slot wiring → 2b retrievers; 2b's
`RetrieverAccessor` + structured errors → 2c episodic memory), and no seam
needed rework. The one thing we'd cut differently: land the shared
"node step" abstraction in 2a rather than extracting it in 2c — the
single-flow agent node grew organically into a function that had to be
refactored while adding features, which is the riskiest kind of change.
