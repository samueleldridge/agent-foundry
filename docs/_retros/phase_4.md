# Phase 4 retro

**What took longer than expected.** The "three scopes, one harness"
property. Tool and project targets were cheap (dispatch through a
ToolRegistry; call `run_project`), but the AGENT scope had no natural
entry point: Phase 3 dissolved the agent step into graph nodes, so
"run the agent in isolation" meant driving the `AgentStepRuntime` slices
(`begin → llm ⇄ tools → finish`, plus the memory `turn`/`turn_end` ring)
with the same routing vocabulary the adapter uses — a ~40-line
label-dispatch loop that is effectively a second, langgraph-free
interpreter of the same state machine. It works (the memory turn-loop
eval passes), but it's a duplication seam: if Phase 7 changes the routing
vocabulary, BOTH the adapter's edge map and the harness driver must move.
Worth revisiting when the multi-agent registry lands — the driver wants
to be owned by `execution.py`, not the eval layer.

**What changed from the plan.** (1) `EvalSpec` stayed in
`foundry.config.schemas` instead of moving to `foundry.eval.schemas` —
it already fed connection health checks since 2a; re-exporting beat
migrating. (2) The judge's cost accounting: docs/40 wants judge calls
budget-bounded AND per-case visible. Judge calls run on the EVAL-scoped
session (so the total budget is enforceable centrally), and their
tokens/cost re-enter the per-case tallies via scorer metadata — slightly
indirect, but it keeps the Scorer protocol pure (`score(case, actual,
config)`) exactly as docs/40 specifies for user scorers. (3) Pin-set
materialization used `git archive | tar` into a temp dir rather than
`git worktree add` — no lock files, no cleanup hooks, read-only by
construction, and it never registers anything against the user's repo.

**What was cheaper than expected.** Cross-vendor judging. Because the
judge is just a `ModelBinding` resolved through the Phase 1 provider
registry, "anthropic agent judged by an openai judge" needed zero new
provider code — one MockTransport serving two hosts proves the whole
gate. Determinism was similarly cheap: temperature forcing + seed
propagation is a 20-line function because `generate()` already takes
per-call settings.

**Deliberate scope holds.** Failure clustering (docs/41) stayed out —
it's the Phase 6 loop's input and docs/03 doesn't list it; the artifact
already carries tags/scorer-results/errors so clustering is a pure
function over `EvalRunResult` later. Standalone tool evals refuse tools
with required connections (structured error) instead of half-shipping a
`test_connection_overrides` surface. Judge calibration is schema-present,
implementation-deferred, exactly as docs/40 open question 2 leans.

**Friction worth recording.** Pydantic field name `pass_` with a `pass`
alias fights mypy's strict constructor checking; `serialization_alias` +
`AliasChoices` validation solved it without loosening the artifact JSON
shape. And an early test discriminated prompt v1/v2 by the string
"get_time" — which appears in BOTH pins' requests via the tool
descriptions block; eval fixtures that key on prompt content must key on
prompt-FILE-unique text.

**Cost of the framework bet this phase:** zero. LangGraph was touched
only through the existing adapter; the eval layer itself is
langgraph-free (lazy import for project scope), which is exactly the
boundary paying off.
