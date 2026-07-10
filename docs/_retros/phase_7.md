# Phase 7 retro

**What took longer than expected.** The state channel redesign. Phase 3's
graph had two channels (`state` holding pre-merged full state, one shared
`conv`), which is exactly wrong for concurrency: two parallel branches
returning full merged states would silently drop each other's writes, and
two agents sharing `conv` would corrupt each other's tool loops. The fix
was structural, not incremental — node methods now return DELTAS, the
`state` channel merges them through the compiled per-field reducers
(LangGraph applies channel reducers sequentially per update, which is
precisely the append/merge accumulate-under-concurrency semantics docs/22
wants), and each agent gets a private `conv__<agent>` channel. The
surprise was how little the Phase 1–2c behaviour moved: because
`apply_delta` was already the merge function, relocating it from inside
the nodes to the channel reducer reproduced byte-identical states and the
whole prior suite passed with only the eval harness's local `apply()`
touched.

**What went better than planned.** HITL. The dreaded part — converting a
tool-raised exception into a durable pause without letting langgraph leak
across the import boundary — collapsed into one `while True` loop in the
adapter's node wrapper: catch `ApprovalRequired`, call `interrupt()`; if
it raises we are genuinely pausing (emit `approval.required`, let it
propagate); if it RETURNS we are replaying after a resume, so record the
resolution and re-invoke the slice with `RunContext.approvals` populated.
LangGraph's interrupt-replay semantics (same call order → same resume
values) matched the docs/32 re-execution contract almost verbatim,
including multi-approval chains for free. The hero integration test
(pause → approvals list → resume --approve → final output reflects it)
passed on the first run.

**What changed from the plan.** Rule-mode handoffs, flow/agent-level
approvals, approval timeouts, and `collect_all` parallel failure mode all
moved to the deferral list — each is additive on machinery that now
exists (predicates, interrupt loop, routers), and none is in the docs/03
exit gate. The one genuine spec cut: graph "complete cover" cannot be
proven at compile for arbitrary predicates (the docs' own example has no
else-edge), so it became compile-checked structure (reachability,
path-to-END, cycles) + a loud first-match/no-match runtime contract.

**What Phase 8 needs to watch.** (1) The resume surface is deliberately
API-shaped: `run_project(approval_response=...)` + the run artifact's
`approval_pending` metadata are exactly what `POST /runs/{id}/resume` and
the WS `ApprovalResponse` handler should call — don't reinvent it. (2)
Node re-execution on resume re-runs sibling tool calls from the paused
round; when the API brings real side-effectful tools, the idempotency
guidance in hitl.py becomes operator-facing documentation. (3) The
dynamic graph-state schema broke checkpoint compatibility with Phase 3
(dev-only, accepted) — Phase 8's Postgres checkpointer should land AFTER
any further channel renames, not before. (4) One pending approval per
resume call is the v1 shape; parallel-branch approvals resolve one at a
time — fine for the CLI, worth an explicit API contract note.
