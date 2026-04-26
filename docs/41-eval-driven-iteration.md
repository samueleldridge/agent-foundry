# 41 — Eval-Driven Iteration

## Purpose

This is the loop that closes the foundry. The eval harness (`40-eval-harness.md`) tells us whether a system works; eval-driven iteration is the workflow that uses that signal to *improve* a system, automatically (via the meta-agent) or manually (via a human reading the same eval results).

This doc specifies: the formal iteration loop, how failures are categorised and read, threshold semantics, iteration budgets, diff-aware re-evals (running only changed paths), eval-set evolution patterns, LLM-judge calibration discipline, and the workflows that gate catalog promotion + production deploy.

The eval harness is in `40`. The meta-agent's iteration logic is in `60-meta-agent.md`. This doc is the consolidating spec for the iteration *workflow* — orthogonal to who's driving it (LLM or human).

Three load-bearing properties:

1. **The loop is the same whether driven by a meta-agent or a human.** Generate → eval → read failures → diagnose → modify → re-eval. The framework provides primitives that make both paths efficient. The meta-agent docs (`60`) layer prompt + tool plumbing on top of these primitives.
2. **Failure categories are typed.** Eval failures aren't "the agent was wrong"; they're "the agent classified `late_amendment` as `partial_settlement` in 8 cases tagged `late_amendment`." Structured failure information is what makes diagnosis efficient.
3. **Iteration is budgeted.** Threshold caps, max iterations, max cost — the loop stops cleanly even if quality plateaus.

## The iteration loop (formal)

```
   ┌─────────────────────────────────────────────────────────────┐
   │  Initial state: target needs to reach quality threshold     │
   │  (project eval ≥ 0.90, tool eval ≥ 0.85, etc.)              │
   └─────────────────────────────────────────────────────────────┘
                            │
                            ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  Iteration N:                                               │
   │                                                             │
   │  1. Run eval against current configuration                  │
   │     → EvalRunResult                                         │
   │                                                             │
   │  2. If score ≥ threshold OR iterations ≥ max OR cost ≥ cap  │
   │     → terminate; return current state + history             │
   │                                                             │
   │  3. Categorise failures (per failure-classification spec)   │
   │     → list[FailureCluster]                                  │
   │                                                             │
   │  4. Diagnose: pick the highest-impact cluster + propose a   │
   │     change                                                  │
   │     - prompt edit (most common)                             │
   │     - tool-binding change (different version, different     │
   │       parameters)                                           │
   │     - flow change (add a worker, change handoff threshold)  │
   │     - state-visibility change (rare)                        │
   │     - new tool scaffold (if missing capability)             │
   │                                                             │
   │  5. Apply the change (creates new agent_version)            │
   │                                                             │
   │  6. Run eval against the new configuration                  │
   │     → EvalRunResult'                                        │
   │                                                             │
   │  7. EvalComparison(prev, current):                          │
   │     - if delta ≤ 0 (regression): rollback, mark this        │
   │       direction exhausted, try alternative diagnosis        │
   │     - if delta > 0: accept, commit, set as baseline for     │
   │       iteration N+1                                         │
   │                                                             │
   │  8. iteration_count += 1; loop                              │
   └─────────────────────────────────────────────────────────────┘
```

The framework provides primitives at every step:
- Step 1 and 6: `foundry.eval.harness`.
- Step 2: `forge` invocation flags or human judgement.
- Step 3: `foundry.eval.failure_clustering` (specified below).
- Step 5: meta-agent's tools (`build_tool`, `pin_version`, `write_file`) or human edits.
- Step 7: `foundry.eval.compare`.

## Failure categorisation

When an eval result has `cases_failed > 0`, the harness produces a structured `FailureClustering` object alongside the standard `EvalRunResult`. Clusters group failures by likely shared cause.

```python
class FailureCluster(BaseModel):
    cluster_id: str
    label: str
    """Human-readable: 'late_amendment classification errors',
    'low confidence on partial settlements', etc."""
    cases: list[CaseResult]
    """Cases in this cluster."""
    suggested_diagnosis: str | None
    """Heuristic suggestion of root cause; meta-agent treats as one
    hypothesis among many."""
    impact: float
    """Aggregate weighted impact on the overall score. Higher = more
    leverage in fixing."""

class FailureClustering(BaseModel):
    eval_run_id: RunId
    clusters: list[FailureCluster]
    unclustered_failures: list[CaseResult]
    """Cases that don't fit a cluster; one-off failures."""
```

Clustering signals (each contributes to grouping):

1. **Tag overlap**: cases with the same `tags` list cluster together.
2. **Scorer-specific failures**: cases that fail the same scorer (e.g., all `result_kind_match` failures) cluster.
3. **Output-field similarity**: when actual outputs share patterns (same wrong root_cause label, same low-confidence threshold), cluster.
4. **Input similarity** (optional, for projects with embedder configured): inputs cluster by semantic similarity using the project's eval embedder.

The clustering algorithm is deterministic for the same `EvalRunResult`. Stable cluster IDs across re-runs make comparison meaningful (cluster `A` shrinking iteration over iteration is a real improvement signal).

## Diagnosis (heuristic + LLM)

The meta-agent's `diagnose_failures` tool reads a `FailureClustering` and produces a structured set of proposals:

```python
class IterationProposal(BaseModel):
    cluster_id: str
    change_kind: Literal[
        "prompt_edit",
        "tool_binding_change",
        "flow_change",
        "state_visibility_change",
        "new_tool_scaffold",
        "agent_split",
    ]
    rationale: str
    """Why this change is expected to fix the cluster."""
    expected_delta: float
    """Estimated score improvement; LLM-derived, not authoritative."""
    risk: Literal["low", "medium", "high"]
    """Probability of regression on currently-passing cases."""
    diff_preview: str
    """The proposed change as a unified diff (for prompt edits)
    or a structured description (for flow/binding changes)."""
```

The meta-agent picks the top-ranked proposal (highest `expected_delta × (1 - risk_weight)`) and applies it. `60-meta-agent.md` details the prompt + tools; this doc specifies the proposal shape.

For human-driven iteration, the same `FailureClustering` is the diagnostic surface — operators read it via `foundry eval show` and decide what to change.

## Threshold semantics

```yaml
# in EvalSpec
threshold: 0.90
```

Three modes for what "threshold" means:

1. **Aggregate threshold** (default): the run's weighted-aggregate score must be ≥ threshold.
2. **Per-cluster threshold** (opt-in): every cluster must have impact ≤ `cluster_impact_max`. Useful when no single failure type can be acceptable, even if aggregate is high.
3. **Per-case threshold** (rare): every case must individually pass. Used for safety-critical evals.

Configuration:

```yaml
threshold: 0.90
threshold_mode: aggregate         # or 'per_cluster' or 'per_case'
cluster_impact_max: 0.05          # for per_cluster mode
```

The meta-agent's iteration termination respects the configured mode — `forge --threshold 0.90 --threshold-mode per_cluster` drives until both aggregate and per-cluster constraints are met.

## Iteration budget

Three caps, all enforced:

```yaml
# In a forge invocation OR an iteration_policy block on the project
max_iterations: 6
max_cost_usd: 20.00
max_wall_time_s: 7200    # 2 hours

# Optional: stop if N consecutive iterations don't improve
no_improvement_after: 3
```

The loop terminates when ANY cap is hit. The meta-agent reports which cap caused termination in its summary. Without iteration budgets, an agent that's just slightly off threshold could iterate forever; budgets force closure with a "best-effort" output if true convergence isn't reached.

`no_improvement_after: 3` is the most useful in practice — when the loop sees no positive delta for 3 iterations, the diagnosis space is likely exhausted; stop and let a human take over.

## Diff-aware re-evaluation

Re-running the full eval after every change is expensive. The harness supports **diff-aware re-eval** to skip cases unaffected by a change.

```python
async def run_eval_diff(
    spec: EvalSpec,
    target: CompiledSystem | BaseAgent | Tool,
    foundry_roots: FoundryRoots,
    baseline: EvalRunResult,
    change: IterationProposal,
) -> EvalRunResult:
    """Run only cases potentially affected by `change`. Cases not
    affected reuse their `baseline` results. Output is a hybrid
    EvalRunResult marked partial=True."""
```

Affected-case heuristics:

| Change kind | Affected cases |
|---|---|
| `prompt_edit` to agent X | all cases that go through agent X |
| `tool_binding_change` for tool Y | cases where the agent calls Y (heuristic: prior eval traces show tool Y in path) |
| `flow_change` (e.g., new edge) | all cases (flow changes affect routing) |
| `state_visibility_change` | all cases (state scope is global) |
| `new_tool_scaffold` (added) | cases where prior runs failed because of missing capability |
| `agent_split` | all cases |

For prompt edits — the most common case — the savings are real: 100-case eval × ~20% affected = 20 cases re-run instead of 100.

The harness emits a `eval.diff_run` event with `cases_run`, `cases_reused`, `partial: true`. Audits show the partial nature; CI gates can be configured to require full eval (no diff) before promotion.

## Eval-set evolution

Eval sets aren't static. Three evolution patterns:

### 1. Adding cases (always safe)

Operators add new cases as new failure modes are discovered in production. The case is a `EvalCase` with stable `id`, expected output, and tags. Adding doesn't invalidate prior comparisons because the comparison framework groups by case id.

The meta-agent CAN propose new cases when it discovers a generalisable failure pattern; the human reviews and accepts via PR. Proposed cases come with an explanation of the pattern they cover.

### 2. Modifying expected outputs (handle carefully)

Sometimes "expected" was wrong, or business logic changed. Modifying `expected` requires:
- Versioning the eval set (the `EvalSpec.schema_version` or content-hash bumps).
- Old eval results become uncomparable to new ones (different `eval_spec_hash`).
- Recommended workflow: rename the case (`<id>_v2`) so the original case+expected are preserved as historical reference; add the new case.

### 3. Retiring cases

When a case becomes obsolete (the underlying business case is gone), retire by setting `skip: true` rather than deleting. This preserves history without affecting runs. Document the reason in `skip_reason`.

### Capturing eval cases from production traffic

A high-leverage pattern: every production run that the operator marks as "interesting" or "wrong" becomes a candidate eval case.

```bash
$ foundry eval capture --project pipeline_recon --since 7d \
    --filter "status=failed OR confidence<0.7" \
    --redact \
    --output evals/captured_q2.yaml
```

The `capture` command:
1. Reads recent run artifacts matching the filter.
2. Extracts `(input, actual_output)` pairs.
3. Applies redaction (per `83-security-guardrails.md`).
4. Outputs a `EvalSpec` with cases marked `expected: <actual>` (operator must review and correct expected values before adding to the canonical eval set).
5. Operator reviews each case; corrects expected; commits to canonical eval set.

This keeps evals in step with production reality; mitigates the "evals diverge from real workload" drift over time.

## LLM-judge calibration

LLM judges are themselves models with biases. Calibration mitigates this by comparing judge scores against human gold-standard scores on a calibration set.

### Calibration workflow

1. **Build a calibration set** (~50 cases): same `(input, expected, actual)` shape as eval cases, plus `human_score: float` (0-1).
2. **Run the judge** against each calibration case → get judge scores.
3. **Fit a regression**: `human_score = a × judge_score + b` (or richer model). Compute residuals and bias.
4. **Apply calibration** in subsequent eval runs: judge scores are corrected by the regression before contributing to aggregates.

Calibration set is stored alongside the eval spec:

```yaml
scorers:
  - kind: llm_judge
    name: classification_judge
    config:
      judge_model_binding: ...
      rubric_template: ...
      calibration_set: calibration/classification_judge_v1.yaml
      calibration_check_interval_s: 86400   # re-fit weekly if data changes
```

If the calibration set's residuals show systematic bias above a threshold, the judge is flagged untrustworthy and the eval reports a warning. Operators react by: switching to a different judge model, refining the rubric, or adding more calibration cases.

Calibration is opt-in. For low-stakes evals, simple LLM-judge without calibration is fine. For production gates (catalog promotion of LLM-judge-evaluated tools), calibration is recommended.

### Multi-judge consensus

Alternative to calibration: run two judges (different vendors) on the same case; aggregate by mean or require agreement above a threshold. Stronger signal at higher cost. Not built-in v1 but easily implemented as a custom scorer composing two `llm_judge` scorers.

## Quality gates

Eval results gate three workflows:

### 1. Catalog promotion

`foundry catalog promote <project>/<artifact>`:
- Runs the artifact's standalone eval.
- Refuses promotion if score < `min_score_for_catalog` (default 0.85).
- `--strict-semver` additionally checks that the promoted version doesn't break input/output schema compatibility with the prior catalog version (warning otherwise).

### 2. Production deploy

`foundry deploy <project>` (admin command, optional in v1):
- Runs the project's last eval result OR re-runs eval pre-deploy (configurable).
- Refuses deploy if score < `production_floor` (configurable per project).
- Records the eval result hash + score in the deployment metadata.

### 3. Meta-agent iteration termination (`foundry forge`)

The forge loop stops when:
- `score ≥ threshold` (per the configured `threshold_mode`), OR
- `iterations ≥ max_iterations`, OR
- `cost ≥ max_cost_usd`, OR
- `wall_time ≥ max_wall_time_s`, OR
- `no_improvement_after` consecutive non-improving iterations.

The forge result reports which condition fired. For successful termination, the result is the latest configuration. For budget-exhausted termination, the result is the best configuration encountered (highest score across all iterations). Either way, the audit trail captures the full trajectory.

## Comparison patterns

Three common shapes, all built on `EvalComparison`:

### A/B between candidate prompts

```bash
$ foundry eval compare --project pipeline_recon \
    --pin-set "main" --pin-set "prompt-experiment-v4"
```

Operator branches off main, tweaks a prompt, runs the same eval, compares. The cluster-level deltas tell them whether the experiment improved the cases they targeted without regressing others.

### Before/after a tool upgrade

```bash
$ foundry eval compare --tool query_snowflake v2 v3
```

Pre-promotion gate. If v3 is worse than v2 on the standalone eval, refuse promotion.

### Trend over time

```bash
$ foundry obs eval-trend --project pipeline_recon --since 30d
```

Every project eval result feeds the trend dashboard. Drift becomes visible: if score is monotonically declining, the eval set may have grown stale or the production environment shifted (model versions, data distribution, etc.).

## Iteration audit

Every iteration the meta-agent (or a human) does is a commit on the project's branch (`foundry/<project>`). The commit message records:

- The iteration number.
- The change kind (prompt_edit / tool_binding_change / etc.).
- The eval score before and after.
- The cluster the change was targeted at.
- The forge run id (if applicable).

`foundry versions <project>` shows the iteration trajectory:

```
$ foundry versions pipeline_recon
commit abc123  2026-04-25 forge   v1 initial scaffold              eval: 0.68 (start)
commit def456  2026-04-25 forge   prompt edit: investigator (late) eval: 0.82 (+0.14, target cluster: late_amendment)
commit ghi789  2026-04-25 forge   prompt edit: resolver            eval: 0.89 (+0.07, target cluster: partial_settlement)
commit jkl012  2026-04-25 forge   tool pin: validate_deltas v2→v3  eval: 0.91 (+0.02, threshold met)
```

Trajectory is searchable via the audit store; the meta-agent uses prior trajectories on similar problems to inform its proposals (a simple form of long-term memory across forge runs).

## Composition with other primitives

| Primitive | How iteration consumes |
|---|---|
| Eval harness | Step 1, 6 of the loop |
| Versioning | Each accepted iteration is a commit; rollback is a pin-edit commit |
| Observability | Iteration generates `forge.iteration` events; per-iteration cost / latency / score in audit store |
| Cost budget | Per-iteration cap + per-forge cap; iteration loop honours both |
| Cache | Semantic cache + tool-result cache make repeated re-evals fast (same cases hitting cache) |

## Workflows the meta-agent doesn't drive (human territory)

Some changes are too high-stakes for autonomous iteration:

- **`flow_change`** beyond simple edge tweaks: restructuring the orchestration topology (turning a graph into a supervisor pattern, adding new agents). Meta-agent flags as a recommendation; human implements.
- **`new_tool_scaffold` for `dangerous: true` tools**: never autonomous (per meta-agent guardrails in `60`).
- **`state_visibility_change`** that broadens write scope: meta-agent flags but doesn't apply; security-sensitive change.
- **Eval-set modifications**: never autonomous. Eval set is the ground truth; the meta-agent moves toward it, not modifies it.
- **Catalog promotion**: never autonomous. Always human-gated (`50-versioning-model.md`).

The meta-agent's prompt explicitly enumerates these (`60-meta-agent.md`).

## Failure modes

| Cause | Surfaced as |
|---|---|
| Eval threshold never met within iteration budget | Forge exits with `best_effort` status; reports highest score and configuration; human review |
| `no_improvement_after` triggered | Same as above; specifically annotated as plateau |
| Diff-aware re-eval misses a case (false negative on "affected") | Detected in periodic full-eval gate; rare; harness logs warnings on heuristic boundary cases |
| Calibration set stale | judge calibration check raises warning; eval result includes the warning |
| Meta-agent diagnoses incorrectly + applies regressing change | EvalComparison detects regression; rollback + try alternative diagnosis; cost is iteration overhead |

## Invariants

1. **Iteration loop terminates.** Bounded by iteration / cost / time caps + plateau detection.
2. **Every iteration is a commit.** No silent edits; trajectory is auditable.
3. **Regressions are rolled back.** A change that decreases score is undone before the next iteration.
4. **Eval sets are immutable within an iteration.** The meta-agent doesn't modify eval sets; the eval is the target, not a parameter.
5. **Threshold mode is honoured.** Aggregate / per-cluster / per-case modes terminate the loop only when their constraint is satisfied.
6. **Diff-aware re-eval is opt-in for non-CI use; full re-eval is required for production gates.** Pre-deploy, run the full eval to be sure.
7. **Calibration warnings surface in the result.** Operators see when judges are drifting.

## Test expectations

### Unit

1. **Failure clustering determinism**: same `EvalRunResult` → same `FailureClustering` (cluster IDs stable).
2. **Threshold modes**: aggregate / per_cluster / per_case modes correctly determine pass/fail given known case scores.
3. **Iteration budget enforcement**: max_iterations / max_cost / max_wall_time / no_improvement each independently terminate the loop.
4. **Regression rollback**: simulated meta-agent applies a change that decreases score → loop rolls back, marks direction exhausted.
5. **Diff-aware affected-case heuristic**: prompt edit → only cases through that agent are flagged affected; full-set diff agrees with full re-eval within 1% delta.

### Contract

1. **Iteration audit completeness**: every iteration produces a commit with the structured commit message.
2. **`pin_set_hash` change per iteration**: each iteration's pin-set hash is unique unless the iteration was a pure rollback.
3. **No eval-set mutation**: eval set hash unchanged across all iterations; if changed, error.

### Integration (Phase 6 exit gate, with `60-meta-agent.md`)

1. End-to-end forge: 6-iteration run on a toy project; reaches threshold within budget; trajectory recorded.
2. Plateau detection: same project with a hard ceiling at 0.85 < threshold 0.90; loop terminates after `no_improvement_after`.
3. Regression detection: forge applies a change that explicitly regresses; rollback fires; alternative diagnosis tried.
4. Diff-aware re-eval correctness: matched against full re-eval over 10 prompt edits in sequence; aggregate score within 0.5% per iteration.

## Open questions

1. **Cross-iteration learning**. Today, each forge run is independent. Learnings from past forges (prompt patterns that worked for similar cluster types) are not formally captured. Lean: defer; meta-agent's prompt can include "recent successful changes for this project" as soft context if the audit store query is cheap. v1.1.
2. **Active eval-set growth**. The `capture` command produces candidate cases; an interactive workflow (`foundry eval review captured.yaml`) for batch review/edit would help. Lean: yes, Phase 8 dev-UX work.
3. **Multi-objective optimisation**. Currently iterations optimise a single aggregated score. Real systems have multiple objectives (accuracy, latency, cost). A Pareto frontier across iterations would surface which trade-offs were taken. Defer; track via observability instead.
4. **Cache-warming for evals**. Evals re-run frequently during forge; semantic-cache warming (per `24-caching-and-optimisation.md` open question 1) would make iterations cheaper. Lean: ship cache-warm as a Phase 9 polish; manageable scope.
5. **Iteration rollback on cluster regression**. Currently rollback fires on aggregate regression. Should it also fire if any specific cluster regresses past a threshold even when aggregate is positive? Lean: opt-in flag (`per_cluster_regression_check: true`); defaults to off.
