# Phase 4 handoff — eval harness + per-artifact evals + version comparison

**Session date:** 2026-07-09
**Branch:** `main`
**Status:** Phase 4 implementation complete; awaiting AI review + operator
manual smoke test. No live API keys in the dev sandbox — every LLM-touching
assertion (project/agent evals, llm_judge, pin-set comparison) verified
against `httpx.MockTransport` per the established pattern.

## Pre-work landed first (from the Phase 3 review)

1. `fix(runtime)`: `SqliteCheckpointStore.save_checkpoint` now takes the
   channel-blob rows and commits checkpoint + blobs in ONE transaction;
   `FoundrySqliteSaver.put` mirrors atomically (a crash mid-mirror leaves
   NOTHING, not a silently-partial checkpoint). `save_blob` removed.
2. `fix(observability)`: `RunCounters.record()` accumulates token/cost
   totals across every LLM call; `run.completed` + the `foundry.run` span
   report sums, not the last call (2-round test pins 140/55).
3. `fix(observability)`: `RunArtifactWriter.next_sequence()` counts only
   complete lines and TRUNCATES a torn trailing line (SIGKILL mid-write)
   so resumed runs neither miscount nor glue events onto garbage.
4. `feat(orchestration)`: node names colliding with an agent's reserved
   sub-node names (`<agent>__llm/tools/finish/turn/turn_end`) are now a
   compile-time `CompileError` (CLI exit 2) in `validate_namespace`; the
   runtime check in `_wire_graph` was removed (compile always precedes).
5. `docs(errata)` in docs/26: in-run episodic ingests are process-local
   across kill+resume; only state-backed memory (working `source_field`,
   semantic `state_field`) survives via the checkpointer.

## What this session built

1. **Schemas.** Config-layer `EvalSpec`/`EvalCase`/`ScorerConfig` grew the
   docs/40 fields: `numeric` scorer kind, per-case `seed`/`skip`/
   `skip_reason`, `case_timeout_s` (default 300s), `case_max_cost_usd`,
   `max_total_cost_usd`, `replicates`, scorer-weight-sum==1.0 and
   unique-case-id validation. The determinism contract is documented on
   the schema (exit-gate item). `foundry.eval.schemas` defines the output
   artifacts (`ScoredCase`, `CaseResult`, `ScorerSummary`,
   `EvalRunResult`, `CaseDelta`, `ComparisonSummary`, `EvalComparison`)
   plus `eval_spec_hash` (16-hex sha256 of `model_dump_json()`).
2. **Scorers.** `ScorerRegistry` (kind → factory), configs validated at
   BUILD time. `exact` (dotted field paths, dict-subset match,
   case/strip options, fuzzy `ratio`|`regex`), `numeric` (eq/ne/gt/gte/
   lt/lte/between + abs/rel tolerance, `expected.` prefix stripped from
   `target_field`), `llm_judge` (judge `ModelBinding` resolved through
   the provider registry — nothing hardcoded; deterministic mode forces
   judge temperature 0 + seed where supported; emits `llm.started/
   completed`; runs on the EVAL session so judge spend hits the eval
   budget; fixed `JudgeOutput {score, rationale}`), `rubric` (criteria
   delegate to exact/numeric/llm_judge; expected sliced by criterion
   name), `user` (entry-point group `foundry.scorers`, ScorerConfig.name
   = entry-point name).
3. **Harness** (`foundry.eval.harness`). ONE `run_eval(spec, target)`
   over three target types: `ToolEvalTarget` (dispatch through a
   one-tool `ToolRegistry` — validation/timeout/retry/events, no agent),
   `AgentEvalTarget` (drives the `AgentStepRuntime` slice loop directly
   — plain AND memory-turn routing — agent in isolation, no flow
   functions), `ProjectEvalTarget` (`run_project(..., checkpointer=
   "none")`, lazily imported so tool evals never touch langgraph). Per
   case: eval-scoped `RunId` + `Session` (case cost budget), case
   timeout, event tee (tokens/cost from `llm.completed` + judge tallies
   from scorer metadata), scorer isolation (a raising scorer records
   0.0 + `eval.scorer.error` warning; the run continues), weighted
   aggregation, per-scorer rollups (avg/pass-rate/p50/p95),
   `max_total_cost_usd` halts and marks remaining cases skipped.
   Everything wrapped in a `foundry.eval` span.
4. **Artifacts.** `~/.foundry/runs/<eval_run_id>/eval_result.json` +
   `cases/<sanitized_case_id>.json`; project/agent evals also append to
   `projects/<name>/.foundry/eval_history.jsonl` (gitignored). Read
   surface for Phase 6: `load_eval_result(id|dir|file)`,
   `list_eval_history(project_dir)`. Artifacts are append-only; a run
   directory is never rewritten (except the same-process artifact_dir
   backfill immediately after creation).
5. **Compare** (`foundry.eval.compare`). `compare_runs` → per-case
   deltas, pass/fail flip detection (regression|fix), per-agent score
   lists; refuses runs of different `eval_spec_hash` (docs/40 invariant
   5). `compare_tool_versions` runs the NEWEST listed version's
   standalone eval against every version. `compare_project_pin_sets`
   materializes each git ref's project subtree (+ `catalog/` when present
   at the ref) into a temp overlay via `git archive` — READ-only; pins
   are never written (Phase 5). Special ref `worktree` = live tree.
   `write_comparison_artifact` persists `eval_comparison.json` under its
   own run id.
6. **Reporter + CLI.** docs/40-shaped tables (+ `--json` typed dumps).
   `foundry eval <project> <eval-set>` / `eval tool <ref>@<v>` /
   `eval agent <project> <agent> [--eval name]` / `eval compare --tool
   <name> v1 v2 ...` / `eval compare --project <path> --pin-set a
   --pin-set b [--eval path]` / `eval show <eval_run_id>` /
   `eval list <project>` / `--fail-under N`. Exit codes: 0 pass, 1 below
   threshold/fail-under, 2 config/infra failure.
7. **Fixtures.** `projects/hello/evals/greeting.yaml` (5 cases, regex
   scorer), `evals/greeting_judged.yaml` (cross-vendor judge),
   `agents/hello_agent/eval/greeting.yaml` (agent scope, documented skip
   case), catalog `word_count@v2` (token-based word counting) whose
   eval.yaml doubles as the shared compare spec (v1 vs v2 flips 2 cases
   fail→pass).

## Deviations from the docs (all deliberate)

1. **`EvalSpec` stays in `foundry.config.schemas`** (docs/40 sketches it
   in `foundry.eval.schemas`); it was already there feeding connection
   health.yaml. `foundry.eval.schemas` re-exports it — one schema, one
   home, no churn for the 2a health-check consumers.
2. **Standalone tool evals cannot bind connections yet.** A tool whose
   spec has non-optional `connections_required` is refused with a
   structured `CompileError` at target build. docs/40's
   `test_connection_overrides` / project-bound-connection path is
   deferred (noted for Phase 5/6); pure tools (word_count, utc_now)
   cover the Phase 4 gates.
3. **Deterministic mode forces temperature 0 unconditionally** (docs/40
   says "unless explicitly overridden per case" — there is no per-case
   settings surface yet). Per-case `seed` is accepted by the schema but
   NOT yet propagated to the provider (spec-level seed is); scorers do
   receive the spec seed. Documented limitation.
4. **`exit 2` heuristic:** "infrastructure failure" = every non-skipped
   case errored. docs/40 names provider auth as the exemplar; a mixed
   run (some cases scored) stays a quality verdict (0/1).
5. **Judge output schema is fixed** (`JudgeOutput{score, rationale}`,
   extra allowed); the configurable `output_schema` + `calibration_set`
   regression are deferred per docs/40 open question 2 (a configured
   calibration_set adds a metadata note, never fails).
6. **Per-agent deltas are single-agent.** `EvalRunResult.metadata
   ["per_agent"]` = `{flow_agent: aggregate}` until Phase 7 grows the
   multi-agent registry; the comparison surface (per_agent lists in
   `ComparisonSummary`) is already shaped for N agents.
7. **Tool-compare spec source = newest LISTED version** (docs/40 says
   "auto-located standalone eval" without picking a version). `--eval
   <path>` overrides. The spec's pinned `target` version is
   informational — running v2's eval against v1 is exactly the point.
8. **`eval show` / `eval list` shipped** (docs/40 CLI recap lists them;
   docs/03's deliverable list doesn't). They're thin reads over the
   artifact surface and the manual smoke tests use them.
9. **Streaming per-case events**: `run_eval(event_sink=...)` tees every
   case's RunEvents synchronously (the docs/40 open question 1 lean),
   but no `eval.case.completed` RunEvent type was added — Phase 8's SSE
   work can add it additively.
10. **Failure clustering (docs/41) not built** — docs/03 § Phase 4 does
    not list it; it's the Phase 6 iteration loop's input. The artifact
    carries everything clustering needs (per-case tags, scorer results,
    errors).

## Interface notes for Phase 5/6 (the artifact contract)

- **Read APIs:** `foundry.eval.load_eval_result(eval_run_id | dir |
  file) -> EvalRunResult`; `foundry.eval.list_eval_history(project_dir)
  -> list[dict]` (one JSONL entry per run: eval_run_id, eval_name,
  scope, target_ref/version, eval_spec_hash, pin_set_hash, score,
  passed, completed_at, artifact_dir).
- **On disk:** `~/.foundry/runs/<eval_run_id>/eval_result.json` (full
  `EvalRunResult`, `pass` serialized via alias) + `cases/<safe_id>.json`
  (per-case `CaseResult`); comparisons under their own run id as
  `eval_comparison.json`. `FOUNDRY_HOME` respected throughout.
- **Catalog promotion gate (Phase 5):** a tool's latest standalone eval
  score is `load_eval_result(...).score`; history filtering by
  `target_ref` prefix. `eval_spec_hash` is the compatibility key.
- **Meta-agent compare (Phase 6):** `compare_runs` is pure — rerun-free
  comparison of already-persisted results works as long as spec hashes
  match. `run_eval(write_artifact=False)` exists for throwaway probes.
- **Budget enforcement for `forge`:** pass `max_total_cost_usd` /
  `case_max_cost_usd` on the spec (or a spec copy) — the harness halts
  and reports partial results; judge spend counts against the total.

## Exit-gate confirmation

| Gate | How verified | Outcome |
|---|---|---|
| hello project eval, 5 cases → score + per-case details | `test_hello_project_eval_five_cases_scores_and_details` (score 1.0, 5 CaseResults with scorer_results, artifact + history) | ✅ (mock) / ⏳ operator |
| tool eval at v1 AND v2; `compare --tool` side-by-side | `test_tool_eval_v1_passes_its_own_eval`, `test_tool_eval_v2_passes_and_v1_fails_v2_contract`, `test_compare_tool_v1_v2_side_by_side_report` (2 fixes flagged, one spec hash) | ✅ |
| end-to-end comparison across two pin-sets, per-agent deltas | `test_pin_set_comparison_reports_per_agent_deltas` (temp git repo, HEAD~1 v1-pin vs HEAD v2-pin, per_agent [0.0, 1.0], worktree untouched) | ✅ (mock) / ⏳ operator |
| llm_judge uses the provider abstraction | `test_llm_judge_uses_provider_abstraction_cross_vendor` (anthropic agent judged by openai binding through ONE MockTransport; judge events + cost folded in) | ✅ (mock) / ⏳ operator |
| artifact under ~/.foundry/runs/<eval_run_id>/ readable by foundry.eval | `test_artifact_written_and_readable` (+ every integration test reads back via `load_eval_result`) | ✅ |
| determinism: same system+eval+seed → same score, documented | `test_deterministic_eval_reproduces_the_score` (2 runs, identical scores, temperature forced to 0); contract documented in `EvalSpec` docstring; seed-unsupported → warning event | ✅ (mock) / ⏳ operator |
| `--fail-under 0.9` non-zero below 0.9 | `test_fail_under_returns_nonzero_below_floor` (exit 1) + `test_fail_under_passes_at_or_above_floor` (exit 0) + all-errored → exit 2 | ✅ |

## Definition-of-done confirmation

- `uv run ruff check src/ tests/` — zero violations.
- `uv run mypy --strict src/foundry/` — passes (159 files).
- `uv run pytest tests/` — 495 passed (prior 429 intact + 9 pre-work +
  57 Phase 4; one Phase 2a test file gained the totals test).
- `run_id`/`eval_run_id` threaded: per-case sessions mint case RunIds,
  the eval session carries the eval_run_id, judge events + `foundry.eval`
  span + artifacts all carry it; no secrets in code/configs/fixtures.
- Scope check: no iteration loop (6), no pin WRITES/rollback (5), no
  meta-agent (6), no multi-agent (7), no failure clustering (6).

**Phase 4 is COMPLETE pending review + operator manual smoke test. Next
session starts Phase 5 (versioning + git backbone) fresh.**
