# 40 — Eval Harness

## Purpose

Eval is how the foundry knows whether an agent, a tool, or a whole multi-agent system actually does what it should. It's also the meta-agent's primary signal — the iteration loop in `41-eval-driven-iteration.md` reads failure cases from eval results and decides what to change. Without a serious eval layer, the meta-agent is steering blind.

This doc specifies the harness: the `EvalSpec` schema, the three eval scopes, scorers, the async runner, the reporter, the `EvalRunResult` shape, cross-version comparison (`EvalComparison`), determinism semantics, CI integration, and CLI surface.

`EvalSpec` schema is in `12-config-and-validation.md`. Per-tool standalone eval lives next to each tool version (`20-tool-system.md`); per-agent eval next to each agent (`21-agent-system.md`); project eval under `projects/<name>/evals/`. This doc is the consolidating spec.

Three load-bearing properties:

1. **Three eval scopes, one harness.** Tool, agent, project — same `EvalSpec` schema, same scorers, same runner, same reporter. Different `scope` field; different inputs/outputs accordingly.
2. **Determinism is a feature, not a coincidence.** When an eval declares `deterministic: true`, the harness sets seeds + uses cache + asserts identical results across runs. Non-determinism is opt-in and reported as a distribution.
3. **Eval results are first-class artifacts.** Every eval run produces a typed `EvalRunResult` stored alongside the run artifact, queryable across versions, comparable via `EvalComparison`. The meta-agent reads these directly.

## Module layout

```
src/foundry/eval/
├── __init__.py            public API (run_eval, run_compare, etc.)
├── schemas.py             EvalSpec, EvalCase, ScorerConfig, EvalRunResult, EvalComparison
├── harness.py             async runner: orchestrates cases, scorers, aggregation
├── compare.py             cross-version + cross-pin-set comparison
├── reporter.py            CLI tables, JSON output, machine-readable formats
└── scorers/
    ├── __init__.py        ScorerRegistry, base Scorer protocol
    ├── exact.py           ExactScorer (equality, with optional fuzzy options)
    ├── llm_judge.py       LLMJudgeScorer (cross-model judge with calibration)
    ├── rubric.py          RubricScorer (per-criterion structured judgement)
    ├── numeric.py         NumericScorer (with absolute/relative tolerance)
    └── user.py            user-plugged scorer entrypoint discovery
```

## Eval is behavioural; pytest is code-level

A common point of confusion worth surfacing up-front: the eval harness is for **behavioural testing** (does this tool / agent / project produce the expected output for this input?). It is NOT a replacement for code-level testing of project-local Python (handler bodies, function-node bodies, custom helpers).

The two surfaces complement:

| Question | Tool |
|---|---|
| Does this tool produce the expected output shape on these 5 representative inputs? | **Eval** (`eval.yaml`) |
| Does this agent reach the right classification on a labelled set of 100 cases? | **Eval** |
| Does the whole multi-agent system handle a representative trade-break workload? | **Eval** (project-level) |
| Does `handler.py`'s SQL builder handle parameter quoting correctly across 17 edge cases? | **Pytest** |
| Does the function-node body raise the right error on a None input? | **Pytest** |
| Does `output_schema.py`'s validator reject malformed amounts as expected? | **Pytest** |
| Did refactoring `handler.py` preserve all the edge-case behaviour? | **Pytest** |

Why eval and not pytest for behavioural testing:
- Eval results are stored as typed artifacts (`EvalRunResult`) enabling `compare_versions` across iterations.
- Eval scores feed catalog-promotion + production-deploy gates (per `52-rollback-and-audit.md`).
- The meta-agent reads eval results directly — pytest results aren't part of its decision surface.
- LLM-using tools / agents have non-deterministic outputs; eval scorers (LLM-judge, rubric) handle this; pytest doesn't.

Why pytest and not eval for code-level testing:
- Code-level edge cases (off-by-one, type coercion, error paths) are exhaustive and granular — eval cases would be hundreds and slow.
- Pytest integrates with IDE test runners, debuggers, coverage.
- Operators already know pytest.
- Foundry ships `foundry.testing` fixtures (Tier 8 — `82-dev-ux.md`) for testing handler.py / function.py / state transitions cleanly.

The mental model: eval is the **contract** (what should this thing DO?); pytest is the **implementation correctness** (does the code that implements the contract have bugs?). Both are needed for production-grade systems.

Detail on testing conventions in `82-dev-ux.md` (Tier 8). What follows in this doc is the eval surface specifically.

## Three scopes

Eval shape doesn't change between scopes. The differences are: where the spec lives, what target is evaluated, what the input/output schemas are.

| Scope | Target | Input | Output | Where eval.yaml lives |
|---|---|---|---|---|
| `tool` | one tool version | `tool.input_schema` instance | `tool.output_schema` instance | `<root>/tools/<name>/v<N>/eval.yaml` |
| `agent` | one agent | state slice matching agent's `read` visibility | agent's `output_schema` instance | `projects/<p>/agents/<a>/eval/<name>.yaml` |
| `project` | whole compiled system | project input schema | project output schema (single or discriminated union) | `projects/<p>/evals/<name>.yaml` |

Each scope's harness flow differs subtly:

- **Tool eval**: input → tool dispatcher (NOT through agent). Connection bindings come from a test fixture or the project's bound connection. No LLM in the path unless the tool is an LLM-using tool.
- **Agent eval**: input state → agent's compiled BaseAgent run → output. State visibility applies; everything outside the agent's `read` is invisible.
- **Project eval**: input → `CompiledSystem.run()` → terminal output. Full multi-agent execution.

The meta-agent uses all three: per-tool to validate scaffolds, per-agent to validate prompt edits, project to validate the whole system before declaring iteration done.

## `EvalSpec` walkthrough

Full schema in `12-config-and-validation.md`. Realistic project-level example:

```yaml
name: pipeline_recon_q1_2026
description: |
  End-to-end eval over 100 historical break investigations from Q1 2026
  with known resolutions.

scope: project
target: pipeline_recon

cases:
  - id: late_amend_clean
    input:
      trade_id: "ABC-001"
      observed_mismatch_usd: 12500.0
      timestamp: "2026-01-15T22:00:00Z"
    expected:
      result_kind: auto_resolved
      root_cause: late_amendment
      recommended_action: auto_resolve
      confidence_min: 0.85
    tags: [late_amendment, auto_resolve]
    weight: 1.0

  - id: ambiguous_partial
    input:
      trade_id: "ABC-002"
      observed_mismatch_usd: 87000.0
      timestamp: "2026-01-22T03:15:00Z"
    expected:
      result_kind: escalated
      root_cause: partial_settlement
      recommended_action: escalate
    tags: [partial_settlement, ambiguous, escalate]
    weight: 2.0

  - id: rounding_obvious
    input:
      trade_id: "ABC-003"
      observed_mismatch_usd: 0.50
      timestamp: "2026-02-01T18:00:00Z"
    expected:
      result_kind: auto_resolved
      root_cause: rounding
      confidence_min: 0.95
    tags: [rounding, auto_resolve]
    weight: 0.5

scorers:
  - kind: exact
    name: result_kind_match
    config: { field: result_kind }
    weight: 0.4

  - kind: exact
    name: root_cause_match
    config: { field: root_cause }
    weight: 0.4

  - kind: numeric
    name: confidence_floor
    config: { field: confidence, op: gte, target_field: expected.confidence_min }
    weight: 0.2

threshold: 0.90
max_parallel: 4
deterministic: true
seed: 42

schema_version: 1
```

A scorer applies to every case; the case's contribution to the overall score is `weight × scorer_weighted_score`. Scorer weights sum to 1.0 (validated at load); case weights normalise across the case set.

## `EvalCase`

```python
class EvalCase(BaseModel):
    id: str
    """Stable, human-readable. Used as the key for per-case results
    across runs; renaming a case loses comparability."""

    input: dict[str, Any]
    """Validated against the target's input schema at load time:
    - tool eval: tool.input_schema
    - agent eval: a TypedDict matching agent's read fields
    - project eval: project input schema"""

    expected: Any
    """Shape depends on the scorers. exact wants a value or partial
    structure; llm_judge wants a rubric; rubric wants a dict of
    criterion → expected. The harness passes expected to each scorer
    along with actual."""

    tags: list[str] = Field(default_factory=list)
    """For filtering / grouping in reports."""

    weight: float = Field(default=1.0, ge=0.0)
    """Contribution to the aggregated score. Heavier weights for
    cases that exercise critical behaviour."""

    seed: int | None = None
    """Per-case seed; overrides the spec's seed for this case.
    Useful for cases that need known-stochastic expected outputs."""

    skip: bool = False
    skip_reason: str | None = None
    """Marked but not run. Useful while debugging without losing the
    case from version control."""
```

Cases must be valid per their input schema at load time — invalid cases fail the eval load with a `ConfigValidationError` naming the failing case `id`. This catches typos before run time.

## Scorers

The ScorerRegistry maps `kind` strings to scorer implementations. The four built-in scorers cover most use cases; user-plugged scorers register via Python entry points.

### `exact`

Equality-or-near-equality. Configurable comparison strategies.

```python
class ExactScorerConfig(BaseModel):
    field: str | None = None
    """Path into the actual output (e.g. 'root_cause', 'investigation.confidence').
    None compares the whole output object."""
    expected_field: str | None = None
    """Path into expected. None means use expected directly."""
    case_sensitive: bool = True
    strip: bool = False
    """Trim whitespace before compare (string only)."""
    fuzzy: FuzzyOptions | None = None
    """Optional fuzzy matching for strings."""
```

Score: 1.0 if equal, 0.0 otherwise. Fuzzy options support edit-distance / token-set ratio / regex match for strings — opt-in.

### `numeric`

Numeric comparison with tolerance.

```python
class NumericScorerConfig(BaseModel):
    field: str
    op: Literal["eq", "ne", "gt", "gte", "lt", "lte", "between"]
    target_field: str | None = None
    target_value: float | None = None
    """Either target_field (read from expected) or target_value."""
    abs_tolerance: float | None = None
    rel_tolerance: float | None = None
    """For 'eq' op only."""
    range: tuple[float, float] | None = None
    """For 'between' op."""
```

Useful for confidence floors, latency caps, cost ceilings.

### `llm_judge`

LLM-based judgement. Used when "correct" is rubric-based and exact match isn't appropriate (free-text outputs, summaries, classifications with grey areas).

```python
class LLMJudgeScorerConfig(BaseModel):
    judge_model_binding: ModelBinding
    """Provider-agnostic; specifies the judge model. Recommended:
    a model from a different vendor than the agent under test, to
    reduce judge/judged co-bias."""

    rubric_template: str
    """Markdown template with placeholders: {input}, {expected}, {actual}.
    Should produce a numeric score 0-1 with rationale."""

    output_schema: str = "schemas.py::JudgeOutput"
    """Pydantic class with at minimum {score: float, rationale: str}.
    Custom schemas can include criterion breakdowns."""

    calibration_set: str | None = None
    """Optional: path to a calibration set of (input, expected, actual,
    human_score) cases used to estimate judge bias. Score is adjusted
    via a regression on the calibration data. Phase 4 polish."""
```

The judge call goes through the provider abstraction; cost is tracked + bounded by the project's `Guardrails.max_cost_usd`. The judge's output IS validated against `output_schema` (so misshapen judge responses surface clearly).

The eval harness emits a `foundry.llm` event for the judge call — judge calls show up in audit just like any other LLM call.

### `rubric`

Multi-criterion structured judgement. The agent's output is scored against a structured rubric; each criterion is independently scored (typically by an LLM judge or a hand-written scorer); criterion scores aggregate per their weights.

```python
class RubricScorerConfig(BaseModel):
    criteria: list[Criterion]

class Criterion(BaseModel):
    name: str
    description: str
    judge_kind: Literal["exact", "numeric", "llm_judge", "user"]
    judge_config: dict[str, Any]
    """Config for the underlying scorer, run on this criterion only."""
    weight: float = Field(default=1.0, ge=0.0)
```

Useful for outputs with multiple quality dimensions (correctness + completeness + tone + safety).

### `user`

Pluggable. Discovered via Python entry points:

```toml
# In a project's pyproject.toml
[project.entry-points."foundry.scorers"]
business_specific_scorer = "my_project.scorers:BusinessSpecificScorer"
```

The scorer class implements:

```python
class Scorer(Protocol):
    name: str
    async def score(
        self,
        case: EvalCase,
        actual: Any,
        config: dict[str, Any],
    ) -> ScoredCase: ...

class ScoredCase(BaseModel):
    case_id: str
    score: float           # 0.0 to 1.0
    pass_: bool            # often `score >= 1.0` or a configurable threshold
    metadata: dict[str, Any]
    """Scorer-specific debug info (e.g. fuzzy distance, rubric breakdowns)."""
```

User scorers respect the same async + cancellation contracts as the built-ins.

## Runner

```python
async def run_eval(
    spec: EvalSpec,
    target: CompiledSystem | BaseAgent | Tool,
    foundry_roots: FoundryRoots,
    overrides: EvalOverrides | None = None,
) -> EvalRunResult:
    ...
```

Sequence:

1. **Resolve target.** For project scope, `target` is a `CompiledSystem`; for agent scope, a `BaseAgent` from the project's compiled registry; for tool scope, a `Tool` instance.
2. **Validate cases.** Each case's `input` validated against the target's input schema. Any failure → `ConfigValidationError` with the case `id`.
3. **Set seed.** If `deterministic: true`, fix the seed across cases (and within each case for stochastic scorers like LLM-judge).
4. **Run cases.** Bounded concurrency via `anyio.create_task_group` capped at `max_parallel`.
5. **Per case**:
   - Mint an eval-scoped `RunId`.
   - Construct or reuse a `Session` (with cost budget = case-level cap or eval-level cap).
   - Invoke target: `tool.handle(input, ctx)` / `agent.run(state, session)` / `compiled.run(input)`.
   - Capture actual output + token usage + cost + latency.
   - For each scorer in spec.scorers: `scorer.score(case, actual, config)` → `ScoredCase`.
   - Aggregate per-case score: `sum(scorer_weight × scorer_score) / sum(scorer_weights)`.
6. **Aggregate.** Across cases: weighted mean.
7. **Build `EvalRunResult`** (spec below).
8. **Write artifact** to `~/.foundry/runs/<eval_run_id>/` and `projects/<name>/.foundry/eval_results/`.

### Determinism

When `deterministic: true`:

- Provider settings: temperature forced to 0 unless explicitly overridden per case.
- Random seeds set: Python `random`, NumPy, and propagated to provider settings where the provider supports `seed`.
- Cache layers MAY be enabled to short-circuit repeated calls (semantic cache + provider prompt cache); resulting in fast repeated runs.
- Same eval + same target + same input + same seed should produce identical scores within scorer-specific tolerance.

When `deterministic: false`:
- The eval runs N times (configurable; default 1) and reports the score distribution.
- Outputs and scores per replicate are stored in the artifact.
- Aggregate score is the mean; CI report includes std dev.

### Per-case timeout + cost limits

Every case respects:
- `EvalSpec.case_timeout_s` (default 300s) — hard wall-clock cap per case.
- `EvalSpec.case_max_cost_usd` (default unbounded) — per-case `Session.cost_budget`.
- `EvalSpec.max_total_cost_usd` (default unbounded) — across all cases; halts run when hit.

These are how the meta-agent's `forge` budget is enforced when it calls the harness internally — the budget is per-eval, not per-iteration.

## `EvalRunResult`

```python
class EvalRunResult(BaseModel):
    eval_run_id: RunId
    eval_spec_ref: str        # path or ref to the EvalSpec
    eval_spec_hash: str       # content hash of the spec
    target_ref: str
    target_version: str       # system_version or tool@version etc.
    pin_set_hash: str         # for project evals; "" for tool/agent
    started_at: datetime
    completed_at: datetime
    duration_ms: int

    cases_total: int
    cases_passed: int          # case score >= threshold
    cases_failed: int
    cases_skipped: int

    score: float               # weighted aggregate, 0.0–1.0
    threshold: float
    passed: bool               # score >= threshold

    per_case: list[CaseResult]
    per_scorer: dict[str, ScorerSummary]
    """Per-scorer rollup: average score, pass rate, p95."""

    cost_total_usd: Decimal | None
    tokens_total: int

    metadata: dict[str, Any]   # arbitrary extra info from the harness


class CaseResult(BaseModel):
    case_id: str
    input_hash: str
    actual: Any                # the target's output
    actual_preview: str | None # truncated for display
    score: float
    pass_: bool
    duration_ms: int
    cost_usd: Decimal | None
    tokens: int
    scorer_results: list[ScoredCase]
    error: dict[str, Any] | None  # FoundryError.to_dict() if the case errored


class ScorerSummary(BaseModel):
    scorer_name: str
    average_score: float
    pass_rate: float
    p50: float
    p95: float
```

Stored as JSON under `~/.foundry/runs/<eval_run_id>/eval_result.json` plus per-case detail under `cases/`.

## Reporter

The reporter formats `EvalRunResult` into:

### CLI table

```
$ foundry eval pipeline_recon evals/q1_2026.yaml
Eval: pipeline_recon_q1_2026 (target: pipeline_recon@a3f8...)
Cases: 100 (passed: 89, failed: 8, skipped: 3)
Score: 0.91 (threshold: 0.90) ✓ PASSED
Duration: 18m 24s; total cost: $4.12

Top failures:
  late_amend_post_cutoff_3                            score: 0.40 (root_cause mismatch)
  partial_fill_with_rounding                          score: 0.50 (recommended_action mismatch)
  fx_settlement_break_eur_usd                         score: 0.60 (low confidence)
  ssi_change_post_amendment                           score: 0.40 (escalated when should auto)

Per-scorer:
  result_kind_match            avg 0.96  pass% 0.96
  root_cause_match             avg 0.86  pass% 0.86
  confidence_floor             avg 0.92  pass% 0.92

Run artifact: ~/.foundry/runs/01JKM4..../eval_result.json
```

`--json` flag dumps the full `EvalRunResult` for machine consumption.

`--fail-under N` exits non-zero if `score < N`. Used in CI gates.

### CI integration

```yaml
# In a CI pipeline
- name: Run end-to-end eval
  run: foundry eval pipeline_recon evals/q1_2026.yaml --fail-under 0.90
```

The harness exits 0 on pass, 1 on score below threshold, 2 on infrastructure failure (provider auth, etc.). CI distinguishes the two.

## `EvalComparison` (cross-version)

Comparing two or more configurations of the same target against the same eval set.

```python
class EvalComparison(BaseModel):
    eval_spec_hash: str
    runs: list[EvalRunResult]    # one per config under comparison
    deltas: list[CaseDelta]
    summary: ComparisonSummary

class CaseDelta(BaseModel):
    case_id: str
    scores: list[float]          # one per run, in the same order as `runs`
    delta: float                 # last - first
    flipped: bool                # True if pass status changed across runs
    flip_direction: Literal["regression", "fix"] | None

class ComparisonSummary(BaseModel):
    label_a: str                 # human-readable name for run A
    label_b: str                 # … run B
    score_a: float
    score_b: float
    delta: float                 # B - A; positive = improvement
    regressions: int             # cases that flipped pass→fail
    fixes: int                   # cases that flipped fail→pass
    cost_a_usd: Decimal | None
    cost_b_usd: Decimal | None
```

CLI surface:

```
$ foundry eval compare --tool query_snowflake v1 v2
Tool: query_snowflake
Eval: <auto-located standalone eval>
Cases: 40

                v1        v2
Score          0.94      0.71  (Δ -0.23)
Pass rate      40/40     28/40 (-12)

Regressions (12 cases flipped pass→fail):
  large_query_truncation
  fx_join_with_amendments
  ...

$ foundry eval compare --project pipeline_recon \
    --pin-set HEAD --pin-set HEAD~5
Project: pipeline_recon
Pin sets: HEAD vs HEAD~5
                HEAD~5    HEAD
Score          0.88      0.91  (Δ +0.03)
Per-agent breakdown:
  break_detector            0.92  0.95  (+0.03)
  root_cause_investigator   0.84  0.92  (+0.08)
  resolver                  0.88  0.86  (-0.02)
```

Used by the meta-agent's `compare_versions` tool (`60-meta-agent.md`) to decide whether an iteration was an improvement.

## Eval result lifecycle

```
foundry eval <project> <eval-set>
   │
   ├── load EvalSpec
   ├── compile target (CompiledSystem / BaseAgent / Tool)
   ├── run cases via harness
   ├── aggregate
   ├── write EvalRunResult artifact
   ├── append entry to projects/<name>/.foundry/eval_history.jsonl
   ├── attach to versions.json metadata for the relevant artifact
   │     (tool: tools/<name>/versions.json
   │      project: .foundry/audit.jsonl with eval_run_id ref)
   └── exit code based on threshold / --fail-under
```

Result artifacts are persistent. `foundry eval list <project>` shows recent results; `foundry eval show <eval_run_id>` shows full details.

## Eval as a quality gate

Three places eval results gate workflows:

1. **Catalog promotion** (`foundry catalog promote`): refuses if the artifact's standalone eval score is below a configurable floor (default 0.85). Per `50-versioning-model.md`.
2. **Production deploy** (`foundry deploy`, optional): refuses if the project's last project-level eval was below `production_floor` (configurable per project, often 0.90).
3. **Meta-agent iteration termination** (`foundry forge`): the meta-agent stops iterating when project eval ≥ `threshold` (per the `forge` invocation).

These gates are configuration, not framework behaviour. Operators pick what they want.

## Determinism and reproducibility (in depth)

The "same input → same output" promise is bounded by:

1. **Model determinism**: at `temperature: 0`, most providers approach determinism but aren't guaranteed. Anthropic / OpenAI both have small floating-point noise that produces occasional drift. Tolerance: aim for ~99% case-level reproducibility; flag the 1% as documented non-determinism.
2. **Seed propagation**: `EvalSpec.seed` flows to provider settings where supported (Anthropic and OpenAI both accept `seed`). For models that ignore `seed`, the result is best-effort.
3. **Cache as determinism aid**: when the semantic cache is enabled and cases are repeated, cached responses are byte-identical. Useful for development; production should not run with eval-determinism-via-cache because real production traffic varies.
4. **Tool-result cache**: same input → same output for `cacheable: true` tools. Eval reproducibility on tool-level evals is naturally tight.
5. **LLM-judge variance**: judges are themselves LLMs; their scores have variance. Mitigate via multi-call averaging (`replicates` param) or via cross-vendor judging.

The eval harness reports an `is_deterministic: bool` per scorer; non-deterministic scorers (LLM-judge without averaging) flag this in the result; the report shows confidence intervals.

## Composition with other primitives

| Primitive | How eval consumes |
|---|---|
| Provider | Eval calls go through the standard provider stack (rate limit, cost budget, observability). |
| Cache | Optional speedup for repeated cases; never required for correctness. |
| Connections | Tool / project evals use bound connections (real or test fixtures via `test_connection_overrides`). |
| Versioning | Eval results attach to artifact versions; `compare` queries across versions cheaply via `pin_set_hash`. |
| Observability | Eval runs emit `foundry.eval` events with case-level rollups; integrated into the same OTel stream. |
| Audit | Eval results land in the audit store alongside run artifacts. |

## CLI surface (recap)

- `foundry eval <project> <eval-set>` — run a project eval.
- `foundry eval tool <ref>@<version>` — run a tool's standalone eval.
- `foundry eval agent <project> <agent_name> [--eval <name>]` — run an agent eval.
- `foundry eval compare --tool <name> <v1> <v2> [<v3> ...]` — cross-version tool compare.
- `foundry eval compare --project <name> --pin-set <a> --pin-set <b>` — cross-pin-set project compare.
- `foundry eval list <project>` — recent eval results for the project.
- `foundry eval show <eval_run_id>` — full per-case detail.
- `foundry eval --fail-under N <project> <eval-set>` — CI gate.

## Failure modes

| Cause | Surfaced as |
|---|---|
| `EvalSpec` invalid (schema, scorer config, etc.) | `ConfigValidationError` at load |
| Case input fails target's input schema | `ConfigValidationError` at load with case `id` |
| Target unresolvable (compile fails) | propagates compile error |
| Scorer raises during scoring | scorer-specific score recorded as 0.0; case marked errored; run continues |
| Per-case timeout | case marked errored; score 0.0 |
| `case_max_cost_usd` exceeded | case errored with `CostBudgetExceeded` |
| `max_total_cost_usd` exceeded | run halts; remaining cases skipped; partial result reported |
| Judge model unavailable | individual scorer errors; case score from non-judge scorers if any; warning event |
| `deterministic: true` + provider doesn't support seed | warning at load; run continues with best-effort determinism |

## Invariants

1. **EvalSpec is content-hashed.** Same spec content → same `eval_spec_hash`, used to compare runs.
2. **Case validation is at load.** Bad inputs don't waste run time.
3. **Cost budget is enforced.** Per-case and total budgets are real caps.
4. **Eval results are append-only.** A new run produces a new artifact; old artifacts are immutable.
5. **`compare` is always against the same `eval_spec_hash`.** Different specs cannot be compared directly; an explicit migration / re-eval is required.
6. **Deterministic mode propagates seed where possible.** And reports best-effort where not.
7. **CI exit codes are stable.** 0 = pass, 1 = below threshold, 2 = infrastructure failure. Distinguishable.

## Test expectations

### Unit

1. **Spec round-trip**: load → dump → re-load equality.
2. **Case validation**: invalid case `input` → `ConfigValidationError` with `case_id`.
3. **Scorer registry**: built-in scorers all instantiable with valid configs; user entry-point discovery works.
4. **Scoring math**: weighted aggregation correct for known case scores + weights.
5. **Threshold semantics**: `score >= threshold` → pass; `<` → fail.
6. **Per-case timeout**: case that sleeps past timeout → errored, score 0.0.
7. **Comparison delta detection**: two runs against same spec hash; case scores compared; flips identified.

### Contract

1. **Reproducibility (best-effort)**: same EvalSpec + same target + same seed → same score within tolerance over 3 trials. Flag if drift > 1%.
2. **CI integration**: `foundry eval --fail-under 0.95` exits non-zero on score 0.94; exits 0 on 0.96.
3. **Hash stability**: `EvalSpec` hashes are deterministic across processes (same input → same hash).

### Integration (Phase 4 exit gate)

1. End-to-end project eval: hello-world project + 5 cases + exact scorer; runs, produces `EvalRunResult` with the expected score.
2. Tool eval: tool with a known input/output mapping + 3 cases; runs against a fake connection; `eval result` reflects scores.
3. `compare --tool`: two versions of a tool with different scores; compare report identifies regressions.
4. `compare --project --pin-set`: same project at two pin sets; per-agent delta breakdown correct.
5. Determinism check: repeat the same project eval 3× with `deterministic: true`; per-case scores within 1% drift.

## Open questions

1. **Streaming eval results.** Currently the harness reports at the end. For long-running evals (1000+ cases), progressive per-case events would be useful. Lean: yes, additive — emit `eval.case.completed` `RunEvent`s; existing OTel + SSE infrastructure supports it.
2. **Judge calibration**. Documented as `calibration_set` field but implementation deferred. Useful for high-stakes use; complex to do well. Defer to Phase 4 polish.
3. **Eval set evolution / schema drift**. When schemas change (e.g. tool input adds a new optional field), old eval cases continue to work because additive changes don't break. When schemas have major bumps, old cases need migration. Tooling: `foundry eval migrate <eval-set>` to apply known migrations from artifact `versions.json`. Lean: build in Phase 5 alongside catalog versioning.
4. **Eval set generation from production traffic.** Capture real production runs as eval cases (with redaction). High value for keeping evals in step with reality. Lean: yes — `foundry eval capture --project <p> --since 7d --redact` to seed cases. Phase 8 dev-UX work.
5. **Cross-eval aggregation**. "Across all my projects, how is end-to-end quality trending?" Probably an observability concern (`80-observability.md`) rather than the harness's. Defer.
