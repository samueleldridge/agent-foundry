# Phase 5 handoff — versioning + git backbone + rollback + catalog promotion

**Session date:** 2026-07-09
**Branch:** `main`
**Status:** Phase 5 implementation complete; awaiting AI review + operator
manual smoke test. Every versioning/promotion integration test runs against
a THROWAWAY temp git repo (the Phase 4 pin-set pattern) — nothing ever
mutates the real workspace.

## Pre-work landed first (from the Phase 4 review)

1. `docs(eval)`: `EvalCase.seed` docstring stops over-promising — it is
   accepted but NOT consumed (spec-level seed only; Phase 4 deviation 3).
2. `docs(phase-4)`: errata — non-deterministic replicates persist
   `replicate_scores` but `actual`/`scorer_results` reflect only the LAST
   replicate.
3. `test(eval)`: judge spend alone trips the eval-session cost budget —
   tool-scope eval (zero target spend) + llm_judge + tiny
   `max_total_cost_usd` → judge blocked PRE-HTTP with `CostBudgetExceeded`
   in the scorer result.

## What this session built

1. **`foundry.versioning.git_backend`** — `GitBackend`: thin SYNC
   `subprocess.run` wrapper (docs/51 sketches async; the Phase 5 consumers
   are sync CLI commands — Phase 6 can wrap in `anyio.to_thread` without a
   surface change). `discover`, `run_git`, `ensure_branch`, `commit`
   (stage-exactly-these-files + commit, atomic: one bad pathspec stages
   nothing), `log`/`show`/`diff`/`revert`/`checkout_paths`,
   `status_porcelain`/`is_dirty(paths=…)`, `ls_files_at`, `rev_parse`/
   `commit_exists`/`branch_exists`, `user_email` (read-only; foundry NEVER
   writes git config). Failures → `GitBackendError` (new, under
   `VersioningError`) with argv/returncode/stderr context.
2. **`foundry.versioning.refs`** — canonical docs/50 3-segment form
   (`<scope>/<kind?>/<name>@<version>`, kind defaults tool) parsed ON TOP
   of `foundry.config.refs.ArtifactRef` (no duplication);
   `check_version_contiguity` (v1..vN, holes → `ConfigError`),
   `latest_version`.
3. **`foundry.versioning.artifacts`** — next tool version dir
   (`create_next_version_dir`, always latest+1), next prompt path
   (`v<N+1>.md`), prompt/dir version listing (both contiguity-checked),
   `versions.json` read/write/append (`append_version_metadata` refuses
   re-recording an existing version — docs/50 invariant 1).
4. **`foundry.versioning.pins`** — `PinTransaction`: stages tool /
   connection / prompt pin edits as SURGICAL text edits (indentation-aware
   block scan; comments + formatting preserved byte-for-byte, so the
   rollback diff is one line for tools, two for prompts), then `apply()`
   validates EVERY staged file against SystemSpec/AgentSpec and only then
   writes (temp-file + `os.replace` per file). A validation failure
   anywhere writes nothing. Prompt pins stage `prompt.version` +
   `prompt.path` together.
5. **`foundry.versioning.rollback`** — `plan_tool_rollback` /
   `plan_prompt_rollback` / `plan_project_rollback` +
   `execute_rollback`. Pre-flight checks per docs/52 with explicit bypass
   classes: `working_tree_clean` (force-able), `correct_branch` (HARD —
   never bypassed; passes with a note when `foundry/<project>` doesn't
   exist, see deviations), `target_exists` (hard), `no_inflight_runs`
   (SKIPPED in v1 — no run registry until Phase 8), `schema_compatible`
   (per-tool; confirm-able; names the breaking movements via
   `versioning.compat`). Per-project mode enumerates files added since the
   target (they get DELETED), restores via `checkout_paths` + explicit
   deletions + ONE commit; any failure restores the subtree to HEAD.
   Every op: ONE ULID run id threaded through the `foundry.rollback` span,
   structlog, and the audit entry id; bypasses land in
   `overrides_used`.
6. **`foundry.versioning.compat`** — contract diffing shared by rollback
   pre-flight and promotion semver detection: input/output JSON-schema
   field diff (removed / shape-changed / new-required → breaking;
   title/description stripped so doc edits are never breaking) +
   connection-slot diff (removed or new-required slot → breaking) + auth
   scheme change → breaking. `foundry.catalog.loader` grew
   `load_tool_contract` / `load_connection_contract` (spec + schema models
   WITHOUT importing handlers/factories).
7. **`foundry.versioning.audit`** — `AuditEntry`/`Operator`/`EvalContext`
   per docs/52 (+ additive `overrides_used`), append-only
   `.foundry/audit.jsonl`, `read_audit_entries` filters
   (type/artifact/since; corrupt lines raise loudly), `resolve_operator`
   (human via `git config user.email`; ci via `CI`/`GITHUB_ACTOR`;
   meta_agent shape reserved for Phase 6's `forge_run_id`).
8. **`foundry.catalog.promote`** — `promote_artifact("<project>/<kind>/
   <name>")`, kinds tool|connection. Gates in order, all pre-write:
   duplicate-content refusal (tree digest with the spec yaml's version
   line neutralised) + defensive dest-exists refusal → eval floor
   (default 0.85; tools run their standalone eval, connections their
   health.yaml) → semver (warn + confirm on breaking; `--strict-semver`
   blocks unless `--allow-breaking`). On success: copy (minus
   `__pycache__`), REWRITE the copied spec's `version:` to the catalog
   number (so `load_tool_version`'s dir/spec consistency check holds),
   append `versions.json` (score, `schema_change`, `breaking_changes`,
   `promoted_by`, `source_ref` — additive `VersionMetadata` fields),
   surgical `index.yaml` insert (comments preserved), ONE commit, audit
   entry in the SOURCE project's log. New error:
   `CatalogPromotionRefused` (under `VersioningError`).
9. **Phase 4 seam CLOSED** — `foundry.eval.load_tool_target(...,
   connections_from=<project dir>)`: a connection-requiring tool's
   standalone eval borrows the project's `system.yaml` bindings
   (`prepare_connection` + `validate_tool_connection_wiring`); the harness
   builds a per-case `SlotConnectionAccessor` (+ pool, closed per case).
   Promotion uses this automatically; a tool required-but-unbound is a
   structured refusal naming the remediation. CLI:
   `foundry eval tool <ref> --project <dir>`.
10. **CLI** — `foundry rollback <project> [--tool <n> | --prompt <agent>]
    --to <v|commit> [--dry-run] [--force] [--yes]` (plan + pre-flight
    printed; confirmation unless `--yes`/`--force`; non-TTY requires
    `--yes`); `foundry versions <project> [--tool <n>]` (commits + per-
    artifact pins/available versions); `foundry diff <project> <ref1>
    <ref2> [--path]`; `foundry catalog promote <target> [--floor]
    [--strict-semver] [--allow-breaking] [--yes] [--notes]`. Exit codes:
    0 applied, 1 refused/aborted, 2 config/unexpected. `catalog
    list`/`show` remain Phase 9 placeholders.

## Deviations from the docs (all deliberate)

1. **`GitBackend` is sync** (docs/51 sketches `anyio.run_process`).
   Rationale above; the meta-agent tool layer (Phase 6) adds asyncing +
   the sandbox (path scoping, forbidden-op guards) per docs/51 — those
   guards are meta-tool-layer by spec, so they are NOT in the backend.
2. **`correct_branch` softens when `foundry/<project>` doesn't exist**:
   repos whose projects live on the default branch (the bundled examples,
   any pre-`foundry project new` layout) pass with an explanatory note.
   When the branch EXISTS, being elsewhere is a hard refusal (not even
   `--force`), per docs/52. `FOUNDRY_PROJECT_BRANCH` env overrides the
   expected name (docs/51 open question 3 lean).
3. **`no_inflight_runs` is recorded as skipped** — there is no run
   registry until Phase 8's serve layer; the technical guarantee (in-
   flight runs are pin-stable, docs/52) is independent of this hygiene
   check.
4. **Audit log is NOT git-tracked**: `.gitignore` has ignored
   `projects/*/.foundry/` since Phase 4 ("runtime state — never
   committed"). docs/52's tamper-evidence-via-git property is therefore
   deferred; in exchange, audit appends never dirty the tree (a
   rollback's own audit write can't fail the next rollback's clean-tree
   check). Revisit if compliance needs bite.
5. **Cache-invalidation acknowledgement is a note + audit rationale**,
   not a `cache.semantic.invalidate` runtime event (docs/52 unit test 5).
   v1 caches are in-process and keyed on `agent_version`/tool version —
   rolled-back entries are unreachable by construction; there is no
   cross-process cache registry for a CLI command to invalidate.
6. **Promotion refuses duplicates by content**, not just by path: the
   destination is always latest+1 (so a legit promotion CANNOT land on an
   existing version); re-promoting unchanged content is refused via a
   version-line-neutralised tree digest.
7. **Connection promotion score is effectively binary** — `run_
   connection_health` raises on any failing case, so a passing health
   check scores 1.0 against the floor. The floor still applies (future
   partial-health backends).
8. **Retriever/agent-template promotion not in v1's surface** (docs/03
   names tools + connections); structured refusal names the limitation.
9. **`git tag` on catalog versions** (docs/51 "hook + tag use") not
   emitted — versions.json + the `catalog(...)` commit message carry the
   same information; tags can be added in Phase 9 polish without
   migration.
10. **`foundry versions` output** is a compact variant of the docs/52
    sketch (commits, prompt pins, tool/connection pins + available
    versions with the active pin starred) — eval scores per commit line
    require the Phase 6 iteration loop's metadata and are omitted.

## Interface notes for Phase 6 (the meta-agent wraps these)

- One import surface: `from foundry.versioning import ...` —
  `GitBackend` (git ops), `PinTransaction` (atomic pin edits),
  `create_next_version_dir`/`next_prompt_path` (scaffolding new
  versions), `plan_*_rollback` + `execute_rollback` (pass
  `operator=resolve_operator(git_email=..., forge_run_id=<forge run>)`
  to mint `kind=meta_agent` audit entries with `human_supervisor`),
  `read_audit_entries` (queryable history), `tool_contract_diff`.
- `RollbackPlan.render()` is CLI-ready text; `plan.checks` is the
  structured pre-flight surface for the meta-agent to reason over.
- `execute_rollback` NEVER commits catalog paths; promotion stays
  human-gated (`foundry.catalog.promote_artifact` is CLI/human-only —
  do not wrap it as a meta-tool).
- Sandbox guards (files inside `projects/<scoped>/` only, branch checks
  before every op, forbidden git verbs) belong in Phase 6's meta-tool
  layer per docs/51 § meta-agent git operations — `GitBackend` is
  deliberately unguarded plumbing.
- Every mutating op returns/records ONE ULID (`AuditEntry.id`) that is
  also the `foundry.rollback`/`foundry.catalog.promote` span's `run_id`
  and the structlog run id — join key across the three surfaces.

## Exit-gate confirmation

| Gate | How verified | Outcome |
|---|---|---|
| Per-tool rollback = single-file pin edit | `test_per_tool_rollback_updates_only_the_pin` — `git diff HEAD~1 --name-only` == `[projects/hello/system.yaml]`, one line changed, other pins untouched | ✅ |
| Per-prompt rollback analogous | `test_per_prompt_rollback_touches_agent_yaml_only` — agent.yaml only; version+path move together | ✅ |
| Per-project rollback atomic | `test_per_project_rollback_restores_subtree_and_removes_added_files` — files added since target REMOVED, pins restored, ONE commit, clean tree; no-op rollback refused with nothing half-applied | ✅ |
| Dirty tree refused unless --force | `test_rollback_refuses_dirty_tree_unless_force` — refusal leaves HEAD+pins+audit untouched; `--force` applies and logs `overrides_used: [working_tree_clean]` | ✅ |
| Promotion copies/refuses-overwrite/indexes/commits | `test_promote_tool_copies_records_indexes_and_commits` + `test_repromoting_identical_content_is_refused` + `test_existing_catalog_versions_are_never_overwritten` | ✅ |
| Promotion blocked below floor | `test_promotion_blocked_below_eval_floor` (score 0.0 vs 0.85; configurable floor proven) | ✅ |
| Audit records op + run id + sha + artifact + operator | asserted inside every rollback/promotion test (ULID id, commit sha, scope, operator kind/email, overrides) | ✅ |
| Schema-incompatible rollback → compile error next run | `test_incompatible_rollback_fails_next_compile` — rollback succeeds (schema warning confirmed), `compile_project` then fails naming tool + unbound slot | ✅ |

## Definition-of-done confirmation

- `uv run ruff check src/ tests/` — zero violations.
- `uv run mypy --strict src/foundry/` — passes (162 files).
- `uv run pytest tests/` — **569 passed** (495 prior + 1 pre-work + 47
  unit + 26 integration for Phase 5).
- Versioning/promotion integration tests: throwaway temp git repos ONLY
  (verified: `git status` clean after full suite).
- `run_id` threaded: ULID per op through span + logs + audit entry.
- No secrets in code/configs/fixtures; no institution names.
- Scope check: no meta-agent git tools (6), no iteration loop (6), no
  deployment rollback (8), no review TUI (9), no audit compaction (9).

**Phase 5 is COMPLETE pending review + operator manual smoke test. Next
session starts Phase 6 (meta-agent core + forge loop) fresh.**
