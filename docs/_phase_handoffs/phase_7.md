# Phase 7 handoff — multi-agent orchestration + HITL

**Session date:** 2026-07-10
**Branch:** `main`
**Status:** Phase 7 implementation complete; awaiting AI review + operator
manual smoke test. No live API keys in the dev sandbox — every gate below
was verified against `httpx.MockTransport` (established pattern), with
scripted turns routed PER AGENT by system-prompt marker so the supervisor
loop's call order is exercised, not assumed. Git-touching tests stayed in
temp repos; the multi-agent fixtures live in tmp_path copies.

## Pre-work landed first (Phase 6 review findings)

One `fix(configurator)` commit:

1. **provider_overrides write bypass closed** — `write_file` now
   content-validates any `agent.yaml`: a parseable YAML carrying
   `model_binding.provider_overrides` is refused with the same
   human-only rationale as `build_agent`'s scaffold guard (recoverable
   `ConfigError`; unparseable YAML passes — it can never compile, so the
   override can never take effect).
2. **`projects/<p>/.foundry/` protected** — joins `evals/` as a write
   VIOLATION (recorded + cancel token fires): the audit log and runtime
   state cannot be silently overwritten by the meta-agent.
3. **Superseded prompt versions frozen** — `agents/<a>/prompts/v<N>.md`
   refuses writes when `N < max(pinned, latest-on-disk)` (recoverable,
   mirroring the frozen `v<N>/` rule for tools); the latest version stays
   writable for the iterate-then-pin loop.

## What this session built

1. **Predicate sandbox** (`orchestration/predicates.py`, docs/30
   § Predicate language) — AST validator + evaluator for `when:`.
   Allowed: comparisons (incl. `in`/`is`), `and`/`or`/`not`, attribute +
   subscript access rooted at `state` (top-level fields validated against
   the schema at compile), literals, list/tuple/set displays, single-arg
   `len`/`bool`/`str`/`int`/`float` and two-arg `isinstance` against a
   type whitelist. Everything else — calls, imports, lambdas,
   comprehensions, f-strings, arithmetic, reflection — raises
   `CompileError` with line/column. Evaluation: empty-builtins `eval`
   over a read-only `StateProxy`; a missing field surfaces as
   `OrchestrationError` carrying the predicate text.
2. **Flow schemas grown** (`config/schemas.py`) — full `HandoffPolicy`
   (`allowed_handoffs`, `force_return_to_supervisor`), `TerminationRule`
   (`when` / `max_hops` / `on_max_hops` / `escalate_to`), `GraphFlow.
   cycles_allowed`, inline `{name: flow}` NESTING on sequential steps /
   parallel branches / supervisor workers, and
   `Guardrails.max_flow_nesting_depth` (default 4).
3. **`patterns.plan_flow` compiles all five patterns** into a langgraph-
   free `FlowPlan` tree (LeafNode / SequentialPlan / ParallelPlan /
   SupervisorPlan / GraphPlan). Compile-time checks: node-reference
   resolution (nested included), one-position-per-node, sub-flow name
   namespace + reserved `__<suffix>` names, nesting depth, supervisor
   `allowed_handoffs` defaults + validation (`escalate_to` must be a
   worker; supervisor must be an agent), graph reachability /
   path-to-END / cycle detection (opt-in via `cycles_allowed`), END is a
   sink.
4. **Handoff tools** (`orchestration/handoff.py`, docs/30) — the compiler
   synthesises `transfer_to_<worker>` (+ `transfer_to_end`) per
   `allowed_handoffs[supervisor]` with the docs' input shape
   (`reason: str, min 10`). They are NOT registry tools: the agent-step
   dispatcher intercepts them, records the route in the conv bundle, and
   the flow router acts on it. The `transfer_to_` prefix is reserved
   against user-authored tool names (invariant 6).
5. **Multi-agent compile** (`compiler.py`) — every agent resolves into a
   `CompiledAgent` (own provider, output schema, retrievers, semantic
   cache, memory, handoff tools); `CompiledProject` carries
   `compiled_agents` + `plan` while the legacy single-agent fields mirror
   the plan's PRIMARY agent (supervisor / last-sequential / parallel
   join-or-then / last END-edge graph agent), so Phase 1–6 call sites and
   the meta-agent's synthetic project run unmodified.
6. **Adapter wiring** (`langgraph_adapter.py`) — recursive FlowPlan →
   StateGraph expansion. Every agent stays the Phase 3 node-sliced
   sub-graph; new router nodes: `<sup>__dispatch` (termination predicate,
   hop cap policy: error / return_partial / escalate-once-then-END,
   handoff events with hop numbers), `<worker>__handoff` (force-return /
   worker-END when allowed + predicate true / escalated-terminal),
   `<node>__route` (first-match edge predicates; no-match →
   `OrchestrationError` naming the predicates). Parallel fan-out from a
   `__enter` no-op; fan-in via a LangGraph waiting edge (`add_edge(list,
   join)`). Nested flows flatten into the same graph with `__exit`
   no-op sinks so a nested supervisor's END returns to its parent.
7. **State channels rebuilt** (`_langgraph_types.make_graph_state`) —
   per-compile dynamic schema: `state` merges node DELTAS through the
   compiled per-field reducers (this is what makes append/merge/lww/
   replace_if_set correct under real branch concurrency), `conv__<agent>`
   private per-agent conversation channels, `route__/decision__<owner>`
   namespaced routing channels, `hops` accumulator, `approvals` merge
   channel, `output`/`outputs` take-last/merge. Node methods in
   `execution.py` now RETURN deltas; the adapter projects each node's
   input to its READ scope before invocation (structural visibility at
   the graph boundary, on top of the runtime's own projection).
8. **HITL** (`orchestration/hitl.py` + adapter + `core`) —
   `ApprovalRequired` grew its typed shape (stable `approval_id`,
   `prompt`, `context`); the dispatch path passes it through unwrapped
   (no retry, no failure event). The adapter converts it into
   `interrupt()` at the node boundary; `approval.required` is emitted
   exactly when the graph actually pauses (interrupt replay on resume
   emits nothing). The run returns `status="approval_pending"` with the
   durable `InterruptPayload`; `run_project(approval_response=...)`
   emits `approval.resolved` and resumes via `Command(resume=...)` — the
   paused node re-executes with resolutions threaded into
   `RunContext.approvals` (`approval_resolved`/`_decision`/`_reason`).
   A handler that re-raises a resolved id → `OrchestrationError`
   ("non-idempotent approval flow").
9. **Guardrails** — `Guardrails.max_iterations` now caps TOTAL agent
   invocations per run (`IterationLimitError` at begin);
   `Guardrails.max_hops` caps total edge traversals at every router
   (`MaxHopsExceededError`); the supervisor's `termination.max_hops`
   applies its `on_max_hops` policy on top.
10. **CLI** — `foundry run` surfaces a pause (prompt + approval id +
    resume instructions; artifact metadata records `approval_pending`,
    the payload, project path, checkpointer). `foundry resume <run_id>`
    shows the pending approval; `--approve` / `--reject --reason "..."`
    resolve and continue (sequence numbers continue; chained approvals
    re-pause cleanly). `foundry approvals list [<project>]` scans the
    local run artifacts.
11. **`projects/team_hello/`** — the example fixture: coordinator
    supervisor + drafter + publisher, with `local/publish_greeting@v1`
    always raising `ApprovalRequired` (stable id = run id + input hash).
    Narrow visibility by design: the publisher sees only `draft`.

## Deviations from the docs (all deliberate)

1. **`handoff_policy.mode: rule` / `hybrid` are deferred** (structured
   CompileError). Rule mode is deterministic routing — the graph
   pattern's job — and it drags per-worker predicate handoffs +
   `force_return_to_supervisor: false` + worker→worker transitions with
   it. All deferred together; v1.1+ backlog.
2. **`force_return_to_supervisor` must stay `true`**; workers may
   terminate via END only when allowed AND `termination.when` fires
   after them. Direct worker→worker edges refuse at compile.
3. **Agent-level (`requires_approval` output field) and flow-level
   (`requires_approval:` on edges) approvals are NOT implemented** —
   docs/32 names three raise sites; the Phase 7 deliverable + exit gate
   name tool-call approval only. The other two are additive on the same
   interrupt machinery (v1.1+ / Phase 8).
4. **Approval `timeout_s` / `on_timeout` are accepted but not enforced**
   — the framework-level timer belongs with the API layer's background
   tasks (Phase 8); v1 approvals wait indefinitely. `foundry approvals`
   ships only `list` (show/approve/reject/stats fold into
   `foundry resume`; the full docs/32 CLI is Phase 8+).
5. **Nested `graph` flows and graph-nodes-as-sub-flows are deferred**;
   graph is top-level with agent/function nodes only. Parallel
   `failure_mode: collect_all` is deferred (default LangGraph semantics =
   effectively cancel_siblings: a failing branch aborts the superstep).
6. **Graph "complete cover" is a runtime guarantee, not a compile
   proof** — statically proving arbitrary predicates mutually exclusive
   + exhaustive is not decidable (the docs' own example has no
   unconditional else). Compile checks reachability / path-to-END /
   cycles; at runtime edges evaluate in declaration order, first match
   wins, and NO match raises `OrchestrationError` naming every
   predicate.
7. **A node occupies exactly one flow position** — repeating an agent in
   two branches would collide its sub-graph node names; loops are the
   supervisor/graph patterns' job. CompileError.
8. **On resume, the whole paused node re-executes** (LangGraph interrupt
   semantics): sibling tool calls dispatched in the same round as the
   approval-raising tool run again. Documented in hitl.py; docs/32
   already demands idempotent handlers. The re-executed LLM round is NOT
   re-called (conv is checkpointed before the tools node).
9. **`transfer_to_end` costs one extra LLM round** — after the END
   handoff confirms, the supervisor is asked once more for its final
   structured answer (docs/31: the supervisor produces the final answer
   when handing off to END). A supervisor that answers directly without
   any handoff also terminates (treated as END).
10. **Supervisor round-trips = 2 hops** (dispatch +1, worker return +1)
    against `termination.max_hops`; `escalate` marks an `escalated`
    channel so the escalation worker's completion goes straight to END
    and a second trip through the cap returns partial instead of
    looping.
11. **Multi-terminal discriminated-union output schemas (docs/31) are
    not generated** — the run's `output` is the last-finished agent's
    output (`outputs` carries per-agent last outputs). Union generation
    belongs with the API-layer schema work (Phase 8).
12. **`RunCompleted.status="approval_pending"` is emitted at pause** —
    the docs' event catalogue has no dedicated "run.paused" event; the
    status field already enumerated `approval_pending`, and a terminal
    event per process keeps the SSE replay contract simple. The resumed
    process emits `run.started` + eventually `run.completed(success)`
    on the SAME sequence stream.
13. **`Command(resume=...)` targets THE pending interrupt** — one
    pending approval at a time is the v1 shape; concurrent approvals in
    parallel branches pause the superstep together and resolve one per
    resume call (each resume re-pauses on the next pending interrupt).

## Interface notes for Phase 8

- The API layer's `POST /runs/{run_id}/resume` should call
  `run_project(..., approval_response={"approval_id", "decision",
  "reason"})` exactly as `cli/resume.py` does; the pending-approval
  surface is `RunResult.pending_approval` (an `InterruptPayload` dump)
  plus the run artifact's `metadata.json` (`status: approval_pending`).
  `resolved_by` (operator identity) should be added when the bearer-auth
  context exists.
- WS/SSE approval flow: `approval.required` / `approval.resolved` events
  are already in the stream with continuing sequence numbers — SSE
  `Last-Event-ID` replay works across the pause unchanged.
- `CompiledProject.agent_map()` / `.flow_plan()` are the multi-agent
  accessors; the legacy single-agent fields remain the primary mirror.
  The meta-agent's `_build_compiled` still constructs the legacy shape
  and runs through the same adapter (its plan synthesises via
  `flow_plan()`).
- The dynamic graph-state schema means checkpoints from Phase 3 are NOT
  resumable across the upgrade (channel names changed: `conv` →
  `conv__<agent>`); dev-only checkpoints, no migration shipped.
- HITL requires a checkpointer; `--checkpoint none` + ApprovalRequired
  fails structurally (interrupt needs a checkpointer). The CLI warns
  when a pause lands on a non-sqlite checkpointer.

## Exit-gate confirmation

| Gate | How verified | Outcome |
|---|---|---|
| Supervisor + 2 workers end-to-end | `test_supervisor_hitl_pause_then_approve_end_to_end` — coordinator → drafter → coordinator → publisher → coordinator → END with typed handoff tools, real routers, sqlite checkpointer | ✅ (mock) / ⏳ operator |
| Workers cannot read fields outside visibility (integration assertion) | same test — every captured publisher request carries exactly `{draft}` (never request/audience), drafter exactly `{request, audience}`; plus the seeded visibility fuzz (unit) | ✅ |
| Parallel fan-out + fan-in, reducer semantics on concurrent writes | `test_parallel_fanout_fanin_reducers_under_real_concurrency` — 3 branches rendezvous on an asyncio.Barrier (sequential regression deadlocks), append/merge/lww/replace_if_set all assert on the merged state the join saw | ✅ |
| HITL: pause mid-run → CLI shows pending → resume --approve → output reflects approval | same hero test — `run paused` surface, `approvals list` row, `resume --approve` completes with `published` in the final summary; `--reject --reason` feeds the reason into the re-invoked tool and final state | ✅ (mock) / ⏳ operator |
| Kill + resume still works in multi-agent runs | `test_kill_and_resume_multi_agent_run` — 401 mid-loop, rerun same id resumes (drafter NOT re-invoked), then the approval pause + resume still complete | ✅ (mock) / ⏳ operator |
| Predicate sandbox: forbidden constructs raise CompileError | unit matrix (17 forbidden shapes incl. imports/comprehensions/lambdas/mutation/reflection) + graph-edge integration check | ✅ |
| max_hops + max_iterations enforced cleanly | max_hops policy matrix (error → RunFailed(MaxHopsExceededError); return_partial → RunCompleted(status=max_hops) with partial state; escalate → forced final handoff then END) + `test_max_iterations_guardrail_caps_agent_invocations` | ✅ |

## Definition-of-done confirmation

- `uv run ruff check src/ tests/` — zero violations.
- `uv run mypy --strict src/foundry/` — passes (167 files).
- `uv run pytest tests/` — **722 passed, 1 skipped** (641 prior intact —
  2 updated for intentional changes: the graph "Phase 7 stub" message is
  now the structural path-to-END check; ApprovalRequired's constructor
  test is dedicated — + 81 new).
- langgraph imports still confined to `langgraph_adapter.py` +
  `_langgraph_types.py` (ruff banned-api + contract test unchanged).
- `run_id` threaded: graph thread id, every router's handoff events,
  approval payloads (`publish-<run_id>-<hash>`), spans, artifacts;
  resumed runs continue the same event sequence.
- No secrets in code/configs/fixtures; no institution names.
- Scope check: no API layer / WS / SSE (8), no observability hardening
  (9), no new meta-agent capabilities (multi-agent forge stays v1.1+).

**Phase 7 is COMPLETE pending review + operator manual smoke test. Next
session starts Phase 8 (API layer + streaming) fresh.**

## Post-review fixes (2026-07-10)

The Phase 7 review found four gaps; all four are closed on `main`:

1. **provider_overrides `extends` bypass (high)** — the Phase 6 raw-text
   guard in `write_file` validated agent.yaml text by filename, so a
   meta-written `agent.yaml` + `extends: base.yaml` pair (overrides in the
   base file) compiled with the smuggled block. The authoritative check now
   lives at the load/compile boundary: `load_agent_spec` / `load_project` /
   `compile_project` / `compare_project_pin_sets` take `meta_authored`;
   every forge compile/eval path (run_eval, compare_versions, the session's
   own baseline/fallback eval) passes `True`, and the VALIDATED spec —
   post-`apply_extends` — rejects `model_binding.provider_overrides` with a
   structured `ConfigValidationError`. Human-authored projects keep the
   field (legal, human-only escape hatch). The raw-text guard remains as
   write-time defense in depth.
2. **Case-insensitive filename bypass (medium)** — `Agent.yaml` /
   `AGENT.YAML` on darwin slipped past the raw-text guard's exact-match
   filename check. The comparison is now case-folded, and the compile-
   boundary check above makes filename tricks irrelevant regardless.
3. **Predicate sandbox dunder reflection (medium)** —
   `state.__class__.__mro__` / `__init__.__globals__` chains compiled and
   evaluated (rooted at `state`; `__class__` resolves on the proxy's type,
   sidestepping `__getattr__`). Any attribute starting AND ending with
   `__`, anywhere in a chain, is now a `CompileError`; adversarial matrix
   extended (bare/nested/mid-chain/post-subscript dunders, `__dict__`,
   `__slots__`, globals chains, dunder-inside-call).
4. **Checkpoint schema fingerprint (low)** — cross-version checkpoints
   (Phase 3's `conv` → Phase 7's `conv__<agent>`) resumed silently with a
   fresh conversation. The SQLite store is now stamped with a fingerprint
   (sha256 of sorted channel names + `CHECKPOINT_SCHEMA_VERSION`); opening
   a database whose checkpoints were written under a different (or
   pre-fingerprint) schema raises `CheckpointSchemaError` telling the user
   to rerun without `--run-id` or clear the checkpoint db. Empty databases
   re-stamp; the in-memory saver is unaffected.

Suite after fixes: **744 passed, 1 skipped** (722+1 baseline intact + 22
new tests); ruff and `mypy --strict` clean; langgraph imports unchanged.
