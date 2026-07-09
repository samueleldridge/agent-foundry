# Phase 6 handoff — the meta-agent (`foundry forge`)

**Session date:** 2026-07-10
**Branch:** `main`
**Status:** Phase 6 implementation complete; awaiting AI review + operator
manual smoke test. Every forge integration test runs against a THROWAWAY
temp git repo with `httpx.MockTransport` serving BOTH LLM sides (scripted
meta-agent turns driving REAL meta-tools; a computed responder for the
forged project) — no API key touches the suite, and the real workspace is
never mutated.

## Pre-work landed first (Phase 5 review findings)

One `fix(versioning)` commit:

1. **rollback.py** — project-mode staging/recovery now uses the COMPUTED
   file set (files tracked at the target + explicit removals) instead of
   `git add -A` / `git clean -fd`, so `--force` on a dirty tree can no
   longer sweep uncommitted operator files into the rollback commit (or
   delete untracked ones during failure recovery).
2. **audit.py** — docstring + corrupt-line error stop claiming the audit
   log is git-versioned tamper evidence (it is gitignored runtime state;
   Phase 5 deviation 4). Guidance now points at cross-checking recorded
   `commit_sha`s.
3. **cli/rollback.py** — an interactive `y` IS the confirmation: it
   satisfies confirm-class pre-flight checks (schema_compatible) without
   a `--yes` rerun.
4. **git_backend.py** — `--end-of-options` guards on every path that
   interpolates a caller-supplied ref (`rev-parse`, `show`, `diff`,
   `ls-tree`, `checkout`, `revert`), so option-injection refs never reach
   git as options. Critical because the meta-tools wrap this surface.
5. **promote.py** — catalog-branch gate (`main` unless
   `FOUNDRY_CATALOG_BRANCH`; passes with the current branch when `main`
   doesn't exist), docstring gate order aligned with the code
   (branch → no-overwrite/duplicate → eval floor → semver), and mid-apply
   failures clean up the half-promoted version dir + `versions.json` +
   `index.yaml`.

## What this session built

1. **`foundry.eval.failure_clustering`** (docs/41) — deterministic
   clustering of an `EvalRunResult`'s failures by (tag set × failing
   scorers), weighted `impact`, stable ids, `render()` for directives.
   (Lives in `foundry.eval` because docs/41 places it there; it was not
   in Phase 4's deliverable list, so it lands here with the loop that
   consumes it.)
2. **`forge.*` events** in `foundry.core.events` — `ForgeStarted`,
   `ForgeIterationStarted/Completed`, `ForgeRollback`, `ForgeTerminated`,
   `MetaAgentViolation`, all in the `RunEvent` union.
3. **`foundry.configurator.tools`** — the 16-tool meta-toolkit (docs/61)
   on the STANDARD `ToolRegistry` dispatch path, all bound to one
   `MetaToolContext` (scoped project, `GitBackend`, forge run id, and
   `ForgeRecords` — the mutable ground truth the session reads back):
   - fs: `read_file` (project + framework + catalog roots; binary
     refusal), `write_file` (project-only, atomic temp+rename, parents
     created).
   - discovery: `list_catalog` / `list_tools` / `list_agents` (tolerant
     of half-scaffolded projects).
   - scaffolds: `build_tool` (NEXT `v<N>/` with the 5-file shape; seeds
     from the prior version when one exists; REFUSES `dangerous: true`
     and catalog-name collisions), `build_agent` (REFUSES
     `provider_overrides`; validates the AgentSpec before writing),
     `new_prompt_version` (copies the pinned prompt; never auto-pins).
   - pinning: `pin_version` over Phase 5's `PinTransaction`; fixed
     key-path set (`tools.<n>.version`, `connections.<n>.version`,
     `prompt.version` — path moves with it); target version must exist.
   - eval: `run_eval` (tool / agent / project scopes), `read_eval_results`,
     `compare_versions` (tool versions; project git refs via
     `compare_project_pin_sets`). Failure clusters rendered into every
     result; eval spend recorded against the forge cost budget (docs/61).
   - versioning: `git_commit` (path- and branch-scoped; structured
     `forge(<scope>): <summary>` message with the
     `Iteration | Eval | Cluster` trailer; ONE audit entry per commit
     with `kind=meta_agent` + `forge_run_id`), `git_show` (branch
     reachability required), `list_versions`, `rollback` (Phase 5
     planners; NO force-class bypasses for the meta-agent).
   - `ensure_allowed_git` refuses forbidden verbs (push/pull/fetch/
     rebase/reset/merge/checkout/switch/tag/config/clean/branch/...)
     and force-class flags BEFORE any subprocess — belt on top of the
     braces that those verbs aren't even exposed as tools.
4. **Sandbox semantics** (docs/60 § Defense in depth): out-of-project
   writes (incl. catalog roots + framework tree), and any write into the
   project's `evals/`, are VIOLATIONS — recorded on `ForgeRecords`, the
   step session's cancel token fires, and the forge terminates with
   `sandbox_violation` (the next LLM round raises `RunCancelled` before
   any HTTP). Recoverable mistakes (missing file, frozen version dir,
   bad key_path) stay plain `ConfigError`s the meta-agent reads and
   adapts to. Frozen-version rule: a `v<N>/` becomes immutable once
   SUPERSEDED; the latest version stays writable (that is how the
   handler-iteration loop works); `versions.json` is always writable.
5. **`foundry.configurator.meta_agent`** — `MetaAgent(BaseAgent)`;
   `bind(forge_run_id, backend)` builds a synthetic `CompiledProject`
   (state = one `directive` field; tools = the meta-toolkit; output =
   `MetaAgentReport`) that executes through the EXISTING
   `run_project` — same LangGraph node slices, checkpointer wiring
   (per-step `memory` checkpointer), same event stream. Prompt at
   `src/foundry/configurator/prompts/v1.md`, pinned by
   `ACTIVE_PROMPT_VERSION`; placeholders rendered at bind time (project,
   roots, catalog index summary). `version` content-hashes (model
   binding, prompt text, toolkit). Default binding:
   `anthropic/claude-opus-4-7`, temperature 0.1, max_tokens 4096.
6. **`foundry.configurator.session`** — `ForgeSession` (docs/62):
   pre-flight (project dir exists, project subtree clean,
   `ensure_branch(foundry/<p>)`, project-scope eval set loads) →
   bootstrap iteration 0 when the project has no agents → improvement
   iterations 1..max_iter, each: cluster last failures → directive →
   ONE `MetaAgent.step` (bounded LLM ⇄ meta-tool loop) → authoritative
   score from the RECORDED project eval (the session runs one itself if
   the meta-agent didn't) → `IterationRecord` → termination checks.
   Termination: `threshold_met` / `max_iter` (explicit best-effort
   detail) / `cost_exhausted` / `wall_time_exhausted` / `plateau`
   (`no_improvement_after`) / `sandbox_violation` / `provider_failure` /
   `eval_infrastructure_failure` / `user_cancelled`. Every termination
   writes `~/.foundry/runs/<forge_run_id>/`: `meta.json`,
   `trajectory.jsonl` (rewritten per iteration — the durable loop
   state), `events.jsonl` (full stream, sequence-continuous across
   iterations), `final_summary.md`. ONE `CostBudget` instance is shared
   by every step session, the meta-agent's provider calls, and eval
   spend.
7. **CLI** — `foundry project new <name>` (skeleton `evals/` + README,
   deliberately NO system.yaml so forge detects bootstrap; committed on
   a fresh `foundry/<name>` branch) and `foundry forge <project>
   --description ... --eval <path> --threshold 0.9 --max-iter 5
   [--max-cost-usd N] [--model p/m] [--no-improvement-after N]
   [--quiet]`. Exit codes: 0 threshold met, 1 best-effort/aborted,
   2 config error. `from foundry import MetaAgent, ForgeGuardrails,
   ForgeResult` works via lazy PEP 562 re-export (so importing
   `foundry.api` never pulls the configurator in as a side effect).
8. **Loader fix discovered by the loop**: `foundry.catalog.loader`
   caches artifact modules by file path, so a rewritten `handler.py`
   would silently re-run stale code on the next eval.
   `invalidate_artifact_module` evicts; `write_file` calls it for any
   rewritten `.py`.

## Deviations from the docs (all deliberate)

1. **Autonomous mode only.** `--interactive` (docs/60/62), discuss mode,
   and `InteractiveCallback` are NOT in v1's surface — the docs/03 Phase 6
   exit gate doesn't require them; noted for the v1.1+ backlog alongside
   `forge --resume` (checkpoint state exists per iteration in
   `trajectory.jsonl` + git, but the resume entry point is unbuilt, as are
   `forge list/cancel/show/replay/trace` and the `.foundry/locks` file).
2. **Iteration = one bounded meta-agent invocation**, not one continuous
   conversation. Each directive carries the score, rendered failure
   clusters, iteration history, and the meta-agent's own `notes` from the
   previous report (docs/60's "failed direction recorded in working
   state"). Checkpointing rides the trajectory artifact + per-step
   LangGraph checkpointer rather than a cross-iteration conversation.
3. **`IterationProposal`/`diagnose_failures` (docs/41) are folded into
   `MetaAgentReport`** — the meta-agent reports change_kind / cluster /
   hypothesis / applied / rolled_back after acting, and the session
   verifies against recorded tool activity. A separate propose-then-apply
   hop adds a round-trip per iteration with no autonomous-mode benefit;
   it becomes necessary WITH interactive mode (deferred together).
4. **Meta-toolkit subset**: `build_function_node`, `build_connection`,
   `list_connections`, `list_function_nodes`, `describe_connection`,
   `check_connection_health` (docs/61) are not shipped — the docs/03
   Phase 6 deliverable list names exactly the 16 shipped tools. The toy
   gate needs no connections; connection scaffolding is v1.1+.
5. **Sandbox violations TERMINATE the forge** (`sandbox_violation`)
   rather than docs/60's "iteration aborts; loop continues" — the task's
   exit gate says writes outside the project "raise and abort", and the
   conservative reading wins for a security boundary. Eval-set writes get
   the same treatment (the target doesn't move).
6. **`write_file` does not auto-commit prior content** on overwrite
   (docs/61 sketch) — the meta-agent's explicit per-iteration
   `git_commit` is the audit unit; auto-commits would break "every
   iteration is exactly one commit".
7. **Version immutability is "frozen once superseded"** — docs/61 says a
   version directory is immutable "once it exists with content", but the
   same doc's workflow has the meta-agent iterating the scaffolded
   handler via `write_file` + eval. The reconciliation: the LATEST
   version is live, earlier versions are frozen, `versions.json` always
   writable.
8. **`compare_versions` scopes are tool + project** (versions / git
   refs). Agent-scope comparison isn't a distinct driver in
   `foundry.eval`; prompt movements are visible through project-scope
   ref comparison.
9. **`foundry eval` gained nothing** — `run_eval`'s meta-tool wraps the
   Phase 4 harness as-is; diff-aware re-eval (docs/41 § Diff-aware) is
   NOT implemented (full re-eval per iteration; toy-scale cost).
10. **`total_tokens`** on `ForgeResult` counts tokens observed via
    `llm.completed` events in the forge stream (meta-agent + evals run
    through the session's sink), not a provider-side ledger.

## Interface notes for Phase 7

- The meta-agent compiles a SYNTHETIC single-agent `CompiledProject`; if
  Phase 7's flow compiler changes `CompiledProject`'s shape, update
  `MetaAgent._build_compiled` alongside it.
- `ForgeRecords` is the extension point for new meta-tools: record every
  mutating action there and the session's trajectory picks it up.
- `foundry.core.events` now carries the forge events; API-layer SSE
  (Phase 8) can stream them as-is.
- Multi-agent forging (supervisor design, `agent_split` change kind) is
  Phase 7+ territory — the prompt already names the change kinds, the
  session doesn't restrict them.
- `foundry obs forge` (docs/62 § reading the trajectory) is Phase 9; the
  artifacts it will read (`meta.json` / `trajectory.jsonl` /
  `events.jsonl` / `final_summary.md`) are shipped now.

## Exit-gate confirmation

| Gate | How verified | Outcome |
|---|---|---|
| Toy forge completes, working agent, improvement trajectory ≥2 iterations | `test_forge_bootstraps_and_improves_to_threshold` — bootstrap 0.5 → 0.833 → 1.0 (threshold 0.9), real evals over real files in a temp repo | ✅ |
| ≥1 catalog tool used (discovery + pinning) | same — `list_catalog` called; `catalog/word_count@v1` pinned in system.yaml; the forged agent CALLS it during evals | ✅ |
| ≥1 local tool via build_tool, standalone eval iterated to pass before wiring | same — digit_sum scaffolded, buggy handler fails its eval (0.0), rewrite passes (1.0) BEFORE the bootstrap commit + project eval | ✅ |
| Each iteration a distinct commit referencing the artifact | same — 3 distinct shas; `forge(qa_bot/agents/qa_agent)` subjects; `Iteration: <forge_run_id> \| Eval \| Cluster` trailer | ✅ |
| Threshold miss after max-iter → clear best-effort state | `test_threshold_miss_exits_best_effort_at_max_iter` — `max_iter` reason, "best effort" detail, committed iteration inspectable on the branch, summary says so | ✅ |
| write_file sandbox: outside project (incl. catalog/, src/foundry/) raises + aborts | unit suite (violations + cancel token) + `test_sandbox_violation_aborts_forge` (no further LLM calls; `sandbox_violation`; file untouched) | ✅ |
| Meta-agent rollback demo: forced regression → compare_versions → revert pin | `test_meta_agent_detects_regression_and_rolls_back` — v2 drops a rule (0.833→0.5), compare (HEAD~1 vs HEAD, both re-evaluated) shows regression, rollback meta-tool restores v1, recovery hits 1.0 | ✅ |
| Cost budget enforced | `test_cost_budget_halts_forge` — tiny cap trips on baseline eval spend; `cost_exhausted` before any meta turn | ✅ |
| Plateau detection | `test_plateau_detection_terminates` — two flat iterations with `no_improvement_after: 2` → `plateau` | ✅ |

## Definition-of-done confirmation

- `uv run ruff check src/ tests/` — zero violations.
- `uv run mypy --strict src/foundry/` — passes (164 files).
- `uv run pytest tests/` — **641 passed** (569 prior + 72 new).
- Forge integration tests: throwaway temp git repos only; `git status`
  clean after the full suite.
- `run_id` threaded: ONE forge_run_id through `forge.*` events,
  structlog, the `foundry.forge` span, every commit trailer, and every
  audit entry (`operator.forge_run_id`); per-iteration step sessions mint
  their own run ids for the meta-agent's own event stream.
- No secrets in code/configs/fixtures (asserted in the hero test against
  the whole trajectory artifact); no institution names.
- Scope check: no multi-agent orchestration patterns (7), no HITL /
  interactive mode (7), no API layer (8), no `foundry obs forge` (9).

**Phase 6 is COMPLETE pending review + operator manual smoke test. Next
session starts Phase 7 (multi-agent orchestration + HITL) fresh.**
