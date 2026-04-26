# 52 — Rollback and Audit

## Purpose

Rollback is the operational verb that makes versioning meaningful — without trustworthy rollback, "we have versions" is just clutter. The audit log is the queryable record of every change that ever happened: who did what, when, why, and what the eval signal said before and after. Together they're the foundry's accountability story.

This doc specifies: the three rollback granularities (per-tool, per-prompt, per-project), the CLI surface, the audit-log format and queryability, the review TUI shape (deferred to Phase 9 but specified here), safety guards, and how rollback interacts with in-flight runs / caches / observability.

The versioning model is in `50-versioning-model.md`. Git operations are in `51-git-backbone.md`. This doc is the consolidating spec for the rollback + audit surface.

Three load-bearing properties:

1. **Per-artifact rollback is a single pin edit.** Rolling back a tool from v3 to v2 is one line in `system.yaml` + one commit. Other agents and tools in the project are unaffected. The bad version (v3) stays on disk; you can roll forward.
2. **Audit is queryable, not just archival.** `.foundry/audit.jsonl` per project lets `foundry obs` answer "what changed last week" / "every change to this tool" / "every iteration on this prompt" without shelling out to git for each query.
3. **Rollback safety is mechanical.** Pre-flight checks (clean working tree, branch correctness, target-version existence). No rollback ever leaves the working tree in a half-applied state.

## The three rollback granularities (recap from `01`)

| Granularity | Mechanism | What's rolled back |
|---|---|---|
| **Per-tool** | Edit pin in `system.yaml` to an earlier version of the tool ref | One tool's behaviour; no other tools, no other agents, no system shape |
| **Per-prompt** | Edit pin in `agent.yaml` to an earlier prompt file (`v<N>.md`) | One agent's prompt; that agent's other config (tools, model, output schema) unchanged |
| **Per-project** | `git checkout <ref> -- projects/<name>/` then commit | The whole project subtree; all artifacts at the state of the chosen commit |

The first two are surgical — they touch one file (the pin manifest). The third is bulk — it touches the whole project tree but is still atomic via a single commit.

## CLI surface

### Listing versions

```
$ foundry versions pipeline_recon
Project: pipeline_recon (branch foundry/pipeline_recon)
Latest commit: f1d1542  2026-04-26  forge: prompt v6 → v7  (eval: 0.93)

Recent commits (10):
  f1d1542  forge   prompt: investigator v6 → v7              (eval: 0.91 → 0.93)
  e7914ac  human   pin: query_snowflake v2 → v3              (eval: 0.93 → 0.93)
  6def016  forge   prompt: resolver v3 → v4                  (eval: 0.89 → 0.91)
  ...

Per-artifact versions (active pin in bold):

  Agents:
    investigator   prompts: v1, v2, v3, v4, v5, v6, **v7**     (active: v7)
    resolver       prompts: v1, v2, v3, **v4**                  (active: v4)
    classifier     prompts: **v1**                              (active: v1)

  Tools (project-local):
    validate_deltas  versions: v1, v2, **v3**, v4              (active: v3)
                                              (v4 exists, not pinned)

  Tools (catalog-pinned):
    query_snowflake  pinned: v2 (catalog has up to v3, additive)
    send_slack       pinned: v1 (latest)

  Connections:
    prod_snowflake   pinned: catalog/snowflake@v2 (latest)
    ops_slack        pinned: catalog/slack_workspace@v1 (latest)
```

`foundry versions` is the single discovery command — both git history (commit summaries) and per-artifact version state (what's pinned where).

### Per-tool rollback

```
$ foundry rollback pipeline_recon --tool validate_deltas --to v2
Resolved: tool 'validate_deltas' currently pinned at v3 in pipeline_recon/system.yaml.
Target: v2.

Changes that will be made:
  projects/pipeline_recon/system.yaml:
    tools.validate_deltas.version: v3 → v2

Pre-flight checks:
  ✓ working tree clean
  ✓ on branch foundry/pipeline_recon
  ✓ target version v2 exists at projects/pipeline_recon/tools/validate_deltas/v2/
  ✓ no in-flight runs on this project
  ✓ schema-compatibility check: v2 input/output schemas compatible with current consumers

Apply? [y/N] y
Applied. Commit: ab12cd34
  rollback(pipeline_recon/system.yaml): pin validate_deltas v3 → v2

Audit entry written. v3 files preserved on disk; rollforward available with --to v3.
```

The pre-flight checks are mandatory; failing any one aborts the rollback before any change is made:

| Check | What it does |
|---|---|
| Working tree clean | No uncommitted changes that would be hard to disentangle from the rollback commit |
| Correct branch | `foundry/<project>` must be checked out (per `51-git-backbone.md`) |
| Target version exists | The target `v<N>/` directory must be on disk |
| No in-flight runs | No active checkpointed runs against the project (rollback shouldn't surprise running workloads) |
| Schema compatibility | Target version's input/output schemas must be consumable by current consumers; warning + confirm if incompatible |

`--force` overrides pre-flight checks (DANGEROUS; logged loudly to audit). Used for emergency rollbacks where the operator accepts the risk.

`--dry-run` shows the planned changes without applying.

### Per-prompt rollback

```
$ foundry rollback pipeline_recon --prompt investigator --to v5
Resolved: agent 'investigator' currently pins prompts/v7.md.
Target: v5.

Changes:
  projects/pipeline_recon/agents/investigator/agent.yaml:
    prompt.version: v7 → v5
    prompt.path: prompts/v7.md → prompts/v5.md

Pre-flight: ✓ all checks pass

Apply? [y/N] y
Applied. Commit: cd34ef56
  rollback(pipeline_recon/agents/investigator): prompt v7 → v5

Semantic cache invalidated for agent 'investigator' (agent_version changed).
```

Note the cache invalidation acknowledgement — semantic cache + agent_version coupling (per `24-caching-and-optimisation.md`) means the rollback automatically invalidates stale cached responses.

### Per-project rollback

```
$ foundry rollback pipeline_recon --to ab12cd34
Target: commit ab12cd34 (5 commits ago, 2 days)
  ab12cd34  forge   prompt: investigator v5 → v6  (eval: 0.86 → 0.89)

Will restore the entire projects/pipeline_recon/ subtree to ab12cd34.

Files affected (15):
  projects/pipeline_recon/system.yaml
  projects/pipeline_recon/agents/investigator/agent.yaml
  projects/pipeline_recon/agents/investigator/prompts/v6.md  (currently exists; will revert)
  projects/pipeline_recon/agents/investigator/prompts/v7.md  (added since; will be REMOVED)
  projects/pipeline_recon/agents/resolver/prompts/v4.md      (added since; will be REMOVED)
  ... 10 more files ...

Pre-flight: ✓ working tree clean / ✓ branch foundry/pipeline_recon / ✓ no in-flight runs

Apply? This is a coarse rollback affecting multiple artifacts. [y/N] y
Applied. Commit: ef56ab78
  rollback(pipeline_recon): bulk to ab12cd34 (15 files; 4 prompt versions removed; 0 tool versions removed)
```

Coarse rollback explicitly enumerates files added since the target commit (which will be removed). Operators see exactly what's deleted; tool/prompt versions that are about to be removed get a separate warning ("v7.md will be removed; you'll lose the ability to roll forward to v7 from this point").

### Diff before commit

```
$ foundry diff pipeline_recon HEAD~3 HEAD --path system.yaml
@@ tools:
   validate_deltas:
     ref: local/validate_deltas
-    version: v2
+    version: v3

$ foundry diff pipeline_recon HEAD~10 HEAD --path agents/investigator/
[shows aggregated diff across the agent's directory]
```

Standard git-diff-shaped output. `--path` filters to a subtree.

### Audit query

```
$ foundry obs audit pipeline_recon --since 7d
Showing 23 audit entries (commits + non-commit ops):

2026-04-26 14:30  forge      prompt v6 → v7         eval 0.91 → 0.93  cluster: late_amend
2026-04-26 14:18  forge      prompt v5 → v6         eval 0.89 → 0.91  cluster: late_amend
2026-04-25 16:42  human      pin: snowflake v2→v3   eval 0.93 → 0.93  reason: faster on FX queries
2026-04-25 12:01  forge      tool v2 → v3 (validate_deltas)  eval 0.91 → 0.93  cluster: rounding
2026-04-25 09:00  rollback   pin: validate_deltas v3 → v2   eval 0.93 → 0.91  reason: regressed partial_settlement
...

$ foundry obs audit pipeline_recon --artifact validate_deltas
Showing 8 entries affecting validate_deltas:
  ...

$ foundry obs audit pipeline_recon --type rollback --since 30d
Showing 3 rollbacks in last 30 days:
  ...
```

Audit queries hit `.foundry/audit.jsonl` directly (not git log) for speed.

## Audit log format

Per project: `projects/<name>/.foundry/audit.jsonl`. Append-only JSONL. Each line is one entry.

```json
{
  "id": "01JKM4ABCDEF",
  "timestamp": "2026-04-26T14:30:18.451Z",
  "commit_sha": "f1d1542abcd...",
  "type": "forge",
  "scope": "pipeline_recon/agents/investigator",
  "summary": "prompt v6 → v7",
  "files_affected": [
    "projects/pipeline_recon/agents/investigator/prompts/v7.md",
    "projects/pipeline_recon/agents/investigator/agent.yaml"
  ],
  "operator": {
    "kind": "meta_agent",
    "forge_run_id": "01JKM4ABCDEF",
    "human_supervisor": "samueleldridge2@gmail.com"
  },
  "eval": {
    "before_score": 0.91,
    "before_run_id": "...",
    "after_score": 0.93,
    "after_run_id": "...",
    "eval_spec_hash": "abc123def456abcd"
  },
  "cluster_id": "late_amendment",
  "rationale": "Strengthened guidance on amendment-timestamp checks. Added explicit example for partial-fill vs rounding distinction.",
  "schema_version": 1
}
```

Schema (`AuditEntry` Pydantic):

```python
class AuditEntry(BaseModel):
    id: str                          # ULID
    timestamp: datetime
    commit_sha: str | None           # None for non-commit operations (cache invalidation, etc.)
    type: Literal["forge", "human", "rollback", "pin", "catalog", "non_commit"]
    scope: str                       # "<project>/<artifact_path>"
    summary: str                     # short, matches commit-message summary line
    files_affected: list[str]
    operator: Operator
    eval: EvalContext | None
    cluster_id: str | None
    rationale: str | None
    schema_version: Literal[1] = 1

class Operator(BaseModel):
    kind: Literal["meta_agent", "human", "ci"]
    forge_run_id: str | None
    human_supervisor: str | None     # email/username when meta_agent invoked under human supervision
    human_email: str | None          # for kind=human

class EvalContext(BaseModel):
    before_score: float | None
    before_run_id: str | None
    after_score: float | None
    after_run_id: str | None
    eval_spec_hash: str | None
```

### What goes in the audit log (vs only git)

| Event | In git log | In audit JSONL | Why |
|---|---|---|---|
| Foundry-managed commit | ✓ | ✓ | git is source of truth for content; audit duplicates for queryability |
| `--no-verify` commit (pre-commit hook bypassed) | ✓ | ✓ + flag | audit captures the bypass for review |
| `git revert` (operator's manual revert outside foundry) | ✓ | ✗ | foundry doesn't intercept manual git ops; audit captured at next foundry cmd |
| Cache invalidation (after rollback) | ✗ | ✓ | non-commit operation; audit captures for explainability |
| `foundry connections health` results | ✗ | ✗ | run-level observability, not audit (different store) |
| `foundry catalog promote` | ✓ (on catalog branch) | ✓ + cross-ref to consuming projects | promotion is a human-gated cross-cutting event |

Rule of thumb: anything that changes versioned state OR is operator-significant goes in audit. Run-level events (LLM calls, tool calls) go in observability.

### Append-only invariant

The audit log is append-only. Lines are never edited or removed. Compaction (rotating older months to gzip archives under `.foundry/audit_archive/<year>-<month>.jsonl.gz`) is a v1.1 feature; for v1, the file grows indefinitely. At ~200 bytes per line, 100 entries/day = ~20KB/day = ~7MB/year. Manageable.

The append-only property makes audit-log integrity simple: any post-hoc edit shows up as a git diff to `.foundry/audit.jsonl` (since the file is itself git-versioned). Tampering is visible.

## Operator identity capture

The `operator` field on each audit entry resolves identity from context:

| Source | Resolution |
|---|---|
| Meta-agent in a `forge` invocation | `kind: meta_agent`, `forge_run_id` populated, `human_supervisor` from CLI auth context |
| Human via `foundry rollback` / `foundry commit` | `kind: human`, `human_email` from `git config user.email` if set (NOT from foundry's git config — from the operator's own git, no foundry mutation) |
| CI / automated scripts | `kind: ci`, `human_email` from CI environment (`GITHUB_ACTOR`, etc.) |
| Unknown / missing context | `kind: human`, `human_email: "unknown"` + audit-system warning |

For compliance use cases (4-eyes / SOX / HIPAA), the `human_supervisor` field is what gets queried — "who actually ran the forge that produced this iteration?"

## Review TUI (Phase 9 deliverable; specified here)

A minimum-viable review surface so operators don't have to memorise git incantations. Built with a TUI library (textual or similar); runs as `foundry review <project>`.

Layout sketch:

```
┌─ foundry review pipeline_recon ────────────────────────────────────────────┐
│                                                                            │
│  Recent commits ─────────────────────────────────────────────────────┐    │
│  > f1d1542  forge  prompt: investigator v6→v7   eval +0.02   2h ago  │    │
│    e7914ac  human  pin: query_snowflake v2→v3   eval ±0.00   1d ago  │    │
│    6def016  forge  prompt: resolver v3→v4       eval +0.02   2d ago  │    │
│    9f8e7d6  rollback: validate_deltas v3→v2     eval -0.02   3d ago  │    │
│                                                                       │    │
│  Selected: f1d1542 ──────────────────────────────────────────────────┘    │
│                                                                            │
│  Diff:                                                                     │
│  @@ projects/pipeline_recon/agents/investigator/agent.yaml @@             │
│  - version: v6                                                             │
│  + version: v7                                                             │
│                                                                            │
│  @@ projects/pipeline_recon/agents/investigator/prompts/v7.md @@          │
│  + # Investigator role                                                     │
│  + ...                                                                     │
│                                                                            │
│  Eval context:                                                             │
│    before: 0.91 (run 01JKL...)                                            │
│    after:  0.93 (run 01JKM...)                                            │
│    cluster: late_amendment (cleared 4/5 cases)                            │
│                                                                            │
│  Operator: meta_agent (forge 01JKM4...)                                    │
│  Human supervisor: samueleldridge2@gmail.com                              │
│                                                                            │
│  [r] rollback to selected   [d] full diff   [e] open in editor   [q] quit │
└────────────────────────────────────────────────────────────────────────────┘
```

Key bindings:
- `↑↓` navigate commits.
- `Enter` expand details.
- `r` rollback to the highlighted commit (per-project rollback; with confirmation prompt).
- `d` full diff in pager.
- `e` open the affected file in `$EDITOR`.
- `/` filter (by type, by date range, by artifact).
- `q` quit.

The TUI is read-only by default except for the rollback action. No edits, no commits — those go through the standard CLI commands.

Phase 9 deliverable; this spec is the design.

## Rollback semantics for in-flight runs

Rollback affects **future** runs (they compile against the new pin set). Existing in-flight runs:

- **Continue with their original pin set.** A run that started against `system_version: A` keeps running against A even if pins are rolled back mid-run. The checkpointer preserves the resolved pin set with the run state.
- **Resume similarly.** A run paused for HITL approval that's resumed after a rollback continues against its original pins.
- **New runs after rollback** use the new (rolled-back) pin set.

This guarantees no in-flight run sees a "frankenstate" of partially-changed pins. The pre-flight check "no in-flight runs" is operator hygiene (avoids surprising users mid-flow); the technical guarantee that runs are pin-stable across rollbacks is independent.

## Rollback semantics for caches

Per `24-caching-and-optimisation.md`:

- **Semantic cache** is keyed on `agent_version`. Per-prompt or per-tool rollbacks change `agent_version`s of affected agents → cache entries for those agents become unreachable (effectively invalidated).
- **Tool-result cache** is keyed on `(tool_ref, tool_version, input_hash)`. Rolling back a tool from v3 to v2 makes v3 entries unreachable; v2 entries (if previously populated) remain valid and are hit on next request.
- **Provider-native prompt cache** is provider-side; not affected by foundry rollback. The next prompt against a rolled-back agent will be a cache miss until re-warming.

The CLI rollback acknowledges cache effects in its output (per the per-prompt rollback example above), so operators aren't surprised by changed performance characteristics post-rollback.

## Rollback safety guards (recap + detail)

The pre-flight check list (mandatory before any rollback applies):

1. **Working tree clean** — `git status` shows no modifications. Override: `--force` (logged).
2. **Correct branch** — current branch is `foundry/<project>`. Override: not allowed; checkout the branch first.
3. **Target exists** — for per-tool / per-prompt: the version directory / file exists. For per-project: the commit exists in the branch's history.
4. **No in-flight runs** — `foundry runs list <project> --status active` returns empty. Override: `--force`.
5. **Schema compatibility** — for per-tool rollback only: target version's input/output schemas must be consumable by current pinned consumers. Warning + confirm if incompatible.

Override flags are logged loudly to audit (`overrides_used: list[str]`) so post-hoc review sees what was bypassed.

## Composition with monitoring

Rollback fires an observability event:

```
foundry.rollback
  attributes:
    project, granularity (tool/prompt/project), target_ref,
    files_affected_count, operator, overrides_used,
    eval_score_before, eval_score_after (if known)
```

Monitoring backends pick this up; alerts on rollbacks let on-call know about production surface changes.

The `foundry obs rollbacks <project> --since 7d` query pulls these events for trending — frequent rollbacks signal an unstable iteration cycle that needs human attention.

## Failure modes

| Cause | Surfaced as |
|---|---|
| Pre-flight fails (any check) | `RollbackError` with the failed check + remediation hint |
| `git checkout` failure (target ref doesn't exist) | `RollbackError("target ref not found in branch history")` |
| File ops fail mid-rollback (rare; e.g., disk full) | `RollbackError`; working tree partially modified — recovery: `git restore .` |
| Cache invalidation fails (Redis down) | warning event; rollback succeeds; cache will be cold until backend recovers |
| Audit log write fails | rollback succeeds (commit in git); audit entry reconstructed on next foundry op (gap visible in audit) |
| Concurrent rollback attempts (two operators) | second attempt fails on "working tree dirty" or "lock held"; clean retry |

## Invariants

1. **Rollback is atomic.** Either the entire change applies (commit + audit + cache acknowledge) or nothing applies.
2. **Rolling back never deletes history.** The bad version remains on disk; the bad commit remains in git log.
3. **Pre-flight checks are mandatory.** `--force` exists but logs the override.
4. **Audit log is append-only.** Tampering is visible (git tracks the audit file).
5. **In-flight runs are pin-stable across rollbacks.** No frankenstate.
6. **Per-artifact rollback affects exactly the named artifact.** Per-tool rollback doesn't touch agents; per-prompt rollback doesn't touch other prompts.
7. **Operator identity is captured on every audit entry.** No anonymous changes.

## Test expectations

### Unit

1. **Pre-flight check enforcement**: each check independently fails the rollback and produces a clear error.
2. **Audit entry shape**: every rollback CLI invocation appends a valid `AuditEntry` to `.foundry/audit.jsonl`.
3. **Operator identity resolution**: meta-agent context produces `kind: meta_agent`; human CLI invocation produces `kind: human` with email from `git config`.
4. **Schema-compatibility check**: a tool rollback to a version with incompatible input schema produces a confirmation prompt; `--force` proceeds.
5. **Cache invalidation acknowledgement**: per-prompt rollback emits `cache.semantic.invalidate` event for the affected agent.
6. **`--dry-run`**: shows planned changes; no commits made; no audit entries written.

### Contract

1. **Audit log append-only**: a test that hand-edits a line in `.foundry/audit.jsonl` and runs any foundry command should detect the tampering (since the file is git-versioned, an unexpected diff is visible to a CI gate).
2. **In-flight run preservation**: a run started before rollback completes against original pins after rollback applies (test with a long-running fixture).
3. **No partial application**: a simulated rollback failure mid-operation leaves the working tree clean (verified by `git status` post-failure).

### Integration (Phase 5 exit gate)

1. End-to-end per-tool rollback: bump validate_deltas v2 → v3, run eval, rollback to v2, verify pin restored + commit created + audit entry + eval score reverts on next run.
2. End-to-end per-prompt rollback: edit and pin investigator prompts/v8, rollback to v6, verify agent_version reverts + cache invalidation event emitted.
3. End-to-end per-project rollback: bulk rollback 5 commits; affected files list correct; new versions added since are removed; audit captures the bulk operation.
4. Override audit: `foundry rollback --force` with dirty working tree → `overrides_used: ['working_tree_dirty']` in audit entry.

## CLI reference (consolidated)

| Command | Purpose |
|---|---|
| `foundry versions <project>` | Show recent commits + per-artifact version state |
| `foundry diff <project> <ref1> <ref2> [--path <p>]` | Diff between commits |
| `foundry rollback <project> --tool <name> --to <version>` | Per-tool rollback |
| `foundry rollback <project> --prompt <agent> --to <version>` | Per-prompt rollback |
| `foundry rollback <project> --to <commit>` | Per-project rollback (atomic across all files) |
| `foundry rollback ... --dry-run` | Preview without applying |
| `foundry rollback ... --force` | Bypass pre-flight checks (logged) |
| `foundry obs audit <project> [--since|--until|--type|--artifact]` | Query audit log |
| `foundry obs rollbacks <project> --since <duration>` | Query rollback events specifically |
| `foundry review <project>` | Interactive TUI for browsing + rolling back (Phase 9) |

## Open questions

1. **Audit log compaction**. Append-only grows forever; archival to gzipped per-month files would be cheap. Lean: yes, ship `foundry audit compact <project>` in Phase 9 polish; default behaviour: compact months older than 12.
2. **Inline approval for high-impact rollbacks**. A per-project rollback affecting 50 files is a big move; should it require multi-step confirmation (display diff, type project name, etc.)? Lean: no by default; operators can use `--dry-run` first. Add a `production_safety_mode: true` flag on the project's `.foundry/config.yaml` that requires it for sensitive deployments.
3. **Audit log signing**. For compliance use cases, signing each audit entry with an HMAC keyed on a deployment secret would prevent post-hoc tampering even with git access. Lean: defer; the git-tracked nature of the file already provides tamper-evidence; signing is overkill for v1.
4. **Cross-project rollback**. A single command that rolls back multiple projects together (e.g., when a shared catalog tool's bug affects three pipelines simultaneously). Lean: defer; document the pattern (script that calls per-project rollback for each); ship if real ops demand.
5. **Rollback notifications**. After a rollback applies, fire a webhook / Slack message to the operator's team. Lean: not built-in; ship as a project-local notification adapter agent, or via lifecycle hook on rollback events.
