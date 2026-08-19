# 62 — Configurator Sessions

## Purpose

A **configurator session** is one execution of the meta-agent against a project — one `foundry forge` invocation, one library `MetaAgent.forge()` call, one TUI session. This doc specifies how sessions are launched, persisted, observed, resumed, and terminated across the three modes (CLI / interactive / library), what artifacts they produce, and how operators consume the output.

The meta-agent itself is in `60-meta-agent.md`. Its tool surface is in `61-meta-tools.md`. The forge loop logic is in `41-eval-driven-iteration.md`. This doc is the consolidating spec for the *session* — the operator's view of the whole thing.

Three load-bearing properties:

1. **Three session modes; one underlying execution.** CLI, interactive, library — they all run the same `MetaAgent.forge()` underneath. Differences are in input/output channels (stdin/stdout vs typed function call vs TUI rendering), not in semantics.
2. **Sessions are checkpointed.** A killed `foundry forge` resumes via `foundry forge --resume <forge_run_id>` from the last completed iteration. Same checkpointer as run-level resume (per Tier 1).
3. **Every session produces a typed trajectory artifact.** The complete iteration history with eval scores, commits, decisions — readable post-hoc, comparable across sessions.

## The three session modes

### CLI mode (autonomous)

Default. `foundry forge` runs the loop end-to-end without human input; output streams to stdout (or `--json` for machine consumption); operator reviews after termination.

```bash
foundry forge pipeline_recon \
  --description "..." \
  --eval evals/q1.yaml \
  --threshold 0.90 \
  --max-iter 6 \
  --max-cost-usd 20 \
  [--model anthropic/claude-opus-4-7] \
  [--seed 42] \
  [--diff-aware-eval]                   # use diff-aware re-eval per 41
```

Stdout format (per the walkthrough in `60-meta-agent.md` § A walked-through forge session): a structured human-readable stream of forge events as they happen, plus a final summary. Each line is prefixed with the originating subsystem (`[forge]`, `[meta-agent]`, `[eval]`).

`--json` mode emits one JSON-serialised `RunEvent` per line (NDJSON). Suitable for piping to log aggregators or CI parsers.

```bash
foundry forge pipeline_recon ... --json | jq -c 'select(.event == "forge.iteration_completed") | {iteration: .iteration_number, score: .eval_score}'
```

### Interactive mode (`--interactive`)

Human checkpoints between iterations. After each proposed change, the meta-agent shows the proposal + diff and waits for operator input.

```bash
foundry forge pipeline_recon --interactive ...
```

The operator's options after each proposal:

| Key | Action |
|---|---|
| `a` | Apply the change (commit; re-eval; continue to next iteration) |
| `s` | Skip this change; mark this hypothesis exhausted; continue with a different proposal |
| `d` | Discuss before applying — opens a side conversation with the meta-agent |
| `q` | Quit forge cleanly; current state preserved on disk; trajectory captured |

The `d` (discuss) action is novel and powerful:

```
[meta-agent] Iteration 3 proposed:
  Change kind: prompt_edit
  Target: agents/investigator/prompts/v3.md → v4.md
  Cluster: low_confidence (impact 0.07)
  Hypothesis: investigator over-flags low confidence on ambiguous cases.
  Diff:
    [shown]

[a] apply  [s] skip  [d] discuss  [q] quit  → d

[discuss mode]
> Why "over-flags" rather than "under-calibrated"?
[meta-agent] Looking at the failing cases: 7 of 9 had confidence in
   [0.65, 0.75]. The expected confidence was >= 0.85. So the agent
   isn't refusing to give high confidence; it's settling at a "safe"
   middle. Calibration drift toward the centre.
> Try a different angle: rather than rewriting the confidence guidance,
   add a "confidence rubric" section that gives concrete examples of
   when 0.85+ is appropriate.
[meta-agent] Reasonable. Revising proposal.
   [revised diff shown]
[a] apply  [s] skip  [d] continue discussing  [q] quit  → a
```

Discuss mode is a side conversation (separate sub-session of the meta-agent's reasoning); the discussion itself is recorded as part of the trajectory artifact. After applying, the standard loop continues.

### Library mode (programmatic)

```python
from foundry import MetaAgent, ForgeGuardrails, ModelBinding

agent = MetaAgent(
    scoped_project="pipeline_recon",
    model_binding=ModelBinding(provider="anthropic", model="claude-opus-4-7"),
    guardrails=ForgeGuardrails(max_iter=6, max_cost_usd=20.0),
)

result = await agent.forge(
    description="...",
    eval_spec_path=Path("projects/pipeline_recon/evals/q1.yaml"),
    interactive=False,
)

print(f"Final score: {result.final_score}")
print(f"Termination: {result.termination_reason}")
for it in result.trajectory:
    print(f"  iter {it.iteration_number}: {it.eval_delta:+.2f} (cluster: {it.cluster_id})")
```

The `ForgeResult`:

```python
class ForgeResult(BaseModel):
    forge_run_id: str
    project: str
    started_at: datetime
    completed_at: datetime
    duration_s: float

    final_score: float
    threshold: float
    threshold_met: bool

    iterations: int
    bootstrap: bool                    # was this a bootstrap forge?

    termination_reason: Literal[
        "threshold_met",
        "max_iter",
        "cost_exhausted",
        "wall_time_exhausted",
        "plateau",
        "user_cancelled",
        "provider_failure",
        "best_effort",
    ]

    trajectory: list[IterationRecord]
    total_cost_usd: Decimal
    total_tokens: int

class IterationRecord(BaseModel):
    iteration_number: int
    proposed_change: IterationProposal      # per 41-eval-driven-iteration.md
    applied: bool                            # false if rolled back
    commit_sha: str | None                   # None if rolled back or not yet committed
    eval_run_id_before: str | None
    eval_run_id_after: str | None
    eval_score_before: float | None
    eval_score_after: float | None
    eval_delta: float | None
    cluster_id: str | None
    duration_s: float
    cost_usd: Decimal
    notes: str | None                        # operator notes from interactive discuss
```

Library mode is what powers automated experiments, A/B comparison of forge configurations, integration with external research tooling.

## Session lifecycle

> **Errata (2026-08, branch stranding):** `foundry project new` commits the skeleton on `foundry/<name>` and then RESTORES the branch the operator started on (the CLI prints "…; back on `<branch>`"); the forge pre-flight checks `foundry/<project>` out itself when the project directory only exists on that branch, so forge launches from any starting branch.

```
foundry forge <project> ...
   │
   ├── PRE: validate project + eval set + framework state
   │     - project directory exists OR will be created
   │     - eval set is loadable
   │     - working tree is clean
   │     - branch is foundry/<project>
   │     - cost budget, iter cap make sense
   │
   ├── INIT: construct meta-agent + forge run id + checkpointer
   │     - mint forge_run_id (ULID)
   │     - configure ForgeGuardrails from CLI args
   │     - construct CostBudget on Session
   │     - construct MetaAgent with bound meta-tools
   │     - register checkpointer for forge_run_id
   │
   ├── EVENT: forge.started
   │     - emit RunEvent + audit log entry
   │
   ├── BOOTSTRAP (if project has no agents yet):
   │     - meta-agent designs system, scaffolds agents/functions/tools
   │     - first eval run establishes baseline
   │     - bootstrap counted as iteration 0
   │
   ├── ITERATE (per 41-eval-driven-iteration.md):
   │     - read failure clustering
   │     - propose change
   │     - (interactive: wait for operator)
   │     - apply change (or skip)
   │     - re-eval
   │     - compare to baseline; commit-and-continue OR rollback
   │     - update trajectory
   │     - checkpoint state
   │
   ├── TERMINATE (when any termination condition fires):
   │     - emit forge.terminated event with reason
   │     - write trajectory artifact to ~/.foundry/runs/<forge_run_id>/
   │     - print summary to stdout (CLI) / return to caller (library)
   │
   └── POST: cleanup
         - close meta-agent's session (cost budget summary captured)
         - emit forge.completed event with final aggregates
```

## Checkpointing + resume

Forge sessions are durable. The `MetaAgent` is a `foundry.Agent`; its `Session.checkpointer` persists state after each iteration. If the process dies mid-forge:

```bash
$ foundry forge pipeline_recon ...
[forge] forge_run_id: 01JKM4ABCDEF
... iteration 1 completed ...
... iteration 2 completed ...
^C
[forge] Cancelled. State checkpointed at iteration 2.
        Resume with: foundry forge --resume 01JKM4ABCDEF

$ foundry forge --resume 01JKM4ABCDEF
[forge] Resuming forge_run_id 01JKM4ABCDEF from iteration 2.
[forge] Loaded state: project pipeline_recon at commit def67890.
        Cost spent so far: $4.12 / $20.00 budget.
        Remaining iterations: 4 / 6.
... iteration 3 ...
```

Resume preserves: forge_run_id, model binding, guardrails, cost-budget remaining, iteration count, trajectory so far. Continues from the next iteration.

Resume across host boundaries (worker death in a multi-host deployment): same as run-level resume per `85-batch-and-throughput.md` — the Postgres checkpointer holds state durably; any worker can resume.

## Trajectory artifact

Stored at `~/.foundry/runs/<forge_run_id>/`. Files:

```
~/.foundry/runs/01JKM4ABCDEF/
├── meta.json                  forge metadata (project, model, guardrails, timing)
├── trajectory.jsonl           one IterationRecord per line, in order
├── events.jsonl               full RunEvent stream from the meta-agent's session
├── interactions/              for interactive mode: discuss conversations per iteration
│   ├── iter_3_discuss.md
│   └── ...
└── final_summary.md           human-readable summary (same as stdout summary)
```

Queryable via `foundry obs forge <forge_run_id>`:

```
$ foundry obs forge 01JKM4ABCDEF
Forge: pipeline_recon
Started: 2026-04-26 14:30 UTC
Completed: 2026-04-26 14:48 UTC (18m)
Reason: threshold_met
Final score: 0.91 (threshold 0.90)
Cost: $4.23 / $20.00

Trajectory:
  bootstrap   →  0.71  (12 files; build_tool × 1; build_agent × 4)
  iter 1      →  0.83  (+0.12) prompt: investigator amendments  [late_amendment]
  iter 2      →  0.86  (+0.03) prompt: resolver partial fills  [partial_settlement]
  iter 3      →  0.91  (+0.05) prompt: investigator confidence  [low_confidence]

Commits on foundry/pipeline_recon (last 4):
  abc12345  forge(.../investigator): prompt v2 → v3
  def67890  forge(.../resolver): prompt v1 → v2
  fedcba01  forge(.../investigator): prompt v1 → v2
  1234abcd  forge(pipeline_recon): bootstrap (12 files)
```

Aggregates across forge runs: `foundry obs forge --project pipeline_recon --since 30d` shows trends — how many forges, average iterations per forge, average cost, success rate (threshold-met / total).

## Multi-project sessions (rare)

A single forge invocation operates on one project. For multi-project scenarios (e.g., applying the same prompt-engineering pattern to three pipelines), the recommended approach is sequential CLI invocations:

```bash
foundry forge pipeline_recon ... --description "prompt-engineering pass for amendment handling"
foundry forge contract_review ... --description "prompt-engineering pass for amendment handling"
foundry forge support_triage ... --description "prompt-engineering pass for amendment handling"
```

Each forge is independent — no cross-project memory or coordination. If common patterns emerge, the operator (not the meta-agent) lifts them to the catalog or to project READMEs.

A "multi-project forge daemon" (per `60-meta-agent.md` open question 4) is deferred. For v1, sequential CLI is the answer.

## CLI commands (the session surface)

| Command | Purpose |
|---|---|
| `foundry forge <project> --description "..." --eval <path> [...flags]` | Start a new forge session (autonomous) |
| `foundry forge <project> --interactive ...` | Start an interactive forge session |
| `foundry forge --resume <forge_run_id>` | Resume an interrupted forge |
| `foundry forge list [--project <p>]` | List recent forge runs (active + completed) |
| `foundry forge cancel <forge_run_id>` | Cancel an active forge (cleans up; preserves trajectory) |
| `foundry forge show <forge_run_id>` | Print the trajectory artifact |
| `foundry forge replay <forge_run_id>` | Replay the events.jsonl stream to stdout (for re-rendering / debugging) |
| `foundry forge trace <forge_run_id> [--iteration N]` | Pretty-print the meta-agent's reasoning trace per iteration: tool calls + arguments + LLM rationale + diff applied + eval delta. Human-readable; faster than parsing `events.jsonl` by hand. |
| `foundry obs forge --project <p> --since <duration>` | Aggregates across forges |

### Reading the meta-agent's reasoning

The trajectory artifact has three layers of detail; pick by need:

| Surface | Grain | When to use |
|---|---|---|
| **Stdout (CLI mode)** OR `final_summary.md` | Iteration-level summary (one line per iteration: cluster, change, delta) | Quick check after the forge completes |
| `foundry forge show <id>` | Iteration-level structured (per `IterationRecord` shape) | Programmatic consumption / scripts |
| `foundry forge trace <id> [--iteration N]` | **Reasoning-level** (every tool call the meta-agent made, every LLM response, every diff applied) — pretty-printed | Debugging an unexpected forge outcome; understanding why a particular iteration went the way it did |
| `events.jsonl` raw | Event-stream level (every `RunEvent` emitted) | Tooling / dashboards / replay |

Example `foundry forge trace` output:

```
$ foundry forge trace 01JKM4ABCDEF --iteration 2

Iteration 2 of forge 01JKM4ABCDEF (project: pipeline_recon)
Started: 2026-04-26 14:33:18 UTC | Duration: 4m 12s | Cost: $1.23

[meta-agent thinking]
  The previous eval (0.83) cleared the late_amendment cluster but
  partial_settlement remains at 11 cases (impact 0.09).
  
  Reading the failing cases via read_eval_results...
  Pattern: 9 of 11 cases involve resolver classifying partial fills
  as rounding errors. Hypothesis: resolver prompt lacks explicit
  rule for distinguishing fill amount from settlement amount.

[tool] read_eval_results(eval_run_id=01JKL5...) → returned (200ms)
[tool] read_file(path=projects/pipeline_recon/agents/resolver/prompts/v1.md) → returned (12ms)
[tool] new_prompt_version(agent='resolver') → created v2.md (8ms)
[tool] write_file(path='.../resolver/prompts/v2.md', content=<3.2k chars>) → ok (15ms)
[tool] pin_version(file='agent.yaml', key_path='prompt.version', new_version='v2') → ok (10ms)
[tool] git_commit(files=[...], message=<structured>) → commit def67890 (340ms)

[meta-agent thinking]
  Change applied. Running eval...

[tool] run_eval(scope='project', target='pipeline_recon', eval_spec_path='evals/q1.yaml') → 0.86 (3min 12s, $0.78)
[tool] compare_versions(scope='project', target='pipeline_recon', refs=['HEAD~1', 'HEAD']) → delta +0.03

[meta-agent decision]
  Δ +0.03 (0.83 → 0.86). Improvement. Accepting iteration.
  Next: low_confidence cluster (impact 0.07).
```

Useful for understanding what the meta-agent actually did + why. Renders from the events.jsonl + per-iteration discuss conversations.

Behaviour notes:
- `forge` requires the working tree to be clean before starting; refuses with a clear error otherwise.
- `forge` requires the current branch to be `foundry/<project>` OR offers to switch (interactive prompt).
- `forge --resume` validates that the project state hasn't drifted since the checkpoint was taken; if it has (someone committed manually), refuses with a recovery hint.
- `forge cancel` allows the in-flight iteration to complete cleanly (don't kill mid-eval); subsequent iterations are aborted; trajectory captured.

## Library API surface

Mirrors the CLI but typed:

```python
from foundry import (
    MetaAgent,
    ForgeGuardrails,
    ForgeResult,
    ModelBinding,
    InteractiveCallback,
)

# Synchronous resume of a previously-started forge:
result = await MetaAgent.resume(forge_run_id="01JKM4ABCDEF")

# Listing forge runs:
runs = await MetaAgent.list_runs(project="pipeline_recon", limit=10)

# Reading a trajectory:
trajectory = await MetaAgent.read_trajectory(forge_run_id="01JKM4ABCDEF")

# Custom interactive callback (for non-CLI UIs):
async def my_callback(proposal: IterationProposal) -> Decision:
    # Show proposal in a custom UI; await operator decision
    ...
    return Decision(action="apply")  # or "skip" / "discuss" / "quit"

result = await agent.forge(
    description="...",
    eval_spec_path=...,
    interactive=True,
    interactive_callback=my_callback,
)
```

The `InteractiveCallback` lets non-CLI UIs (Jupyter widgets, web UIs, Slack bots) integrate without going through stdin.

## Notebook ergonomics

In a Jupyter notebook:

```python
from foundry import MetaAgent

agent = MetaAgent.for_project("pipeline_recon")  # convenience constructor
result = await agent.forge(
    description="..."[, ...]
)

# Inspect result interactively:
result.summary_dataframe()            # pandas DataFrame of trajectory
result.eval_progression_plot()        # matplotlib plot of score per iteration
result.cost_progression_plot()
result.diff_for_iteration(2)          # unified diff for iteration 2's commit

# Compare two forge runs:
comparison = MetaAgent.compare_runs("01JKM4...", "01JKM5...")
comparison.plot_score_progression()
```

These convenience methods are notebook-only conveniences (`pandas`, `matplotlib` optional deps); the core library doesn't depend on them. Available via `pip install foundry[notebook]`.

## Concurrent forge sessions (anti-pattern)

Two `foundry forge` invocations against the same project at the same time is an operator error:
- Both will try to commit to `foundry/<project>` branch; second commit will conflict.
- Eval runs may interleave; trajectory becomes unreadable.
- Cost budgets fight each other.

The framework detects: a project-level lock at `.foundry/locks/<project>.lock` is acquired at forge start. Second invocation blocks until the lock releases (with a warning) OR fails with `--no-wait`.

For genuine multi-operator workflows: separate branches off `foundry/<project>` (per `51-git-backbone.md` open question 5), each with their own forge runs in dedicated branches; merge back manually.

## Failure modes

| Cause | Surfaced as |
|---|---|
| Project doesn't exist + no `--description` | `ForgeError("project must exist OR --description must be provided for bootstrap")` |
| Working tree dirty | `ForgeError("working tree has uncommitted changes; commit/stash first")` |
| Wrong branch | offer to switch (interactive) or `ForgeError` |
| Eval set unloadable | `ConfigLoadError` |
| `forge_run_id` collision (resume of an already-completed run) | `ForgeError("run already completed; start a new forge")` |
| Resume across schema migration | `ForgeError("framework version changed; cannot resume")` — start a new forge |
| Lock held by another process | block with timeout (default 30s) OR `ForgeError("lock held by pid X")` with `--no-wait` |
| Cost budget exhausted mid-iteration | iteration completes if possible; `forge.terminated(reason="cost_exhausted")` |
| Provider failure | retries per `RetryPolicy`; if exhausted: `forge.terminated(reason="provider_failure")` |
| Operator `Ctrl-C` | `forge.terminated(reason="user_cancelled")`; trajectory preserved |
| Eval infrastructure failure (e.g., test DB down) | `forge.terminated(reason="eval_infrastructure_failure")` |

Every termination produces a `forge.terminated` event + saves the trajectory artifact. The forge_run_id is logged to stdout for follow-up inspection.

## Composition with observability + audit

Every forge session emits:
- `forge.started` event at session start.
- `forge.iteration_started` / `forge.iteration_completed` per iteration.
- `forge.proposal` per change proposal (interactive: with operator decision).
- `forge.rollback` if a regression triggers rollback.
- `forge.terminated` at session end.

Plus all underlying events from the meta-agent's `BaseAgent.run()` (LLM calls, meta-tool calls, agent.* events).

Audit log entries (per `52-rollback-and-audit.md`):
- One per commit (the meta-agent's iteration commits).
- One per non-commit operation that mutates state (cache invalidations, etc.).
- The `forge_run_id` is a foreign key on every entry, allowing audit queries scoped to a specific forge.

## Invariants

1. **One forge run per (project, time)**. Lock prevents concurrency.
2. **Trajectory artifact written for every termination**, regardless of reason.
3. **Resume preserves identity**: `forge_run_id` unchanged; iteration count continues; cost budget continues from where it was.
4. **Three modes share execution**: CLI / interactive / library all dispatch through the same `MetaAgent.forge()`; behaviour identical modulo input/output channels.
5. **Operator can always interrupt cleanly**. `Ctrl-C` / `q` in interactive / `forge cancel` from library or CLI all produce the same orderly shutdown.
6. **No partial commits leak**. If a forge dies mid-commit, the next forge sees a clean state (the in-flight commit either completed before the death or isn't there).
7. **Trajectory is the source of truth for what happened**. Stdout output is for humans; trajectory.jsonl is for tooling.

## Test expectations

### Unit

1. **CLI argument parsing**: required + optional flags accepted in expected combinations; missing required raises clear error.
2. **Interactive callback signature**: registered callback is invoked at every proposal; return value (apply/skip/discuss/quit) routes correctly.
3. **Library API surface**: `MetaAgent.forge()`, `MetaAgent.resume()`, `MetaAgent.list_runs()`, `MetaAgent.read_trajectory()` all return typed results.
4. **Trajectory JSONL format**: written entries are valid JSON; round-trip parse → dump matches.
5. **Lock acquisition**: two concurrent forge invocations on same project; second waits then fails after timeout.

### Contract

1. **Mode equivalence**: same forge invocation in CLI vs library produces identical `ForgeResult.trajectory` (modulo timing fields).
2. **Resume correctness**: kill forge after iteration 2; `forge --resume <id>` continues from iteration 3 with state matching iteration 2 endpoint.
3. **Resume across processes**: kill one process; another process resumes; total state consistent.
4. **Audit completeness**: every iteration in the trajectory has a corresponding audit entry; audit-only and trajectory-only entries don't exist.

### Integration (Phase 6 exit gate)

1. End-to-end CLI forge: as in `60-meta-agent.md` walkthrough; produces correct trajectory.jsonl + final_summary.md.
2. End-to-end interactive forge: 3 iterations, operator approves 2 + skips 1; trajectory reflects the decisions.
3. End-to-end library forge: as in library example above; result returned with full trajectory.
4. Resume after interrupt: runs to threshold across the kill; total iterations + cost preserved.
5. Concurrent attempt: second forge blocks; clear UX.

## Open questions

1. **Forge-as-a-service / daemon mode**. A long-running daemon serving multiple forge requests over an API. Lean: defer; in v1, forge is a foreground / batch process.
2. **Cross-forge analytics**. "What are my most-effective prompt-engineering patterns across all forges?" — read-only analytics over trajectory.jsonl files. Lean: yes, simple `foundry forge analytics` Phase 9 polish; useful pattern recognition.
3. **Forge templates**. `foundry forge --template pipeline_recon_starter` to bootstrap from a known-good template. Lean: yes, ship as `catalog/forge_templates/` Phase 5+.
4. **Real-time observability piping**. Pipe forge events into the institution's observability stack (Datadog, Langfuse) in real time, not just at termination. Lean: already supported via OTel — every `forge.*` event is in the standard event stream. Document the pattern.
5. **Forge dry-run**. Run the meta-agent's reasoning + proposed changes without actually committing or running evals. Useful for "what would the meta-agent do?" exploration. Lean: yes, `foundry forge --dry-run`; the meta-agent emits proposals but skips apply + eval; produces a "trajectory of intentions" artifact. Phase 9 polish.
