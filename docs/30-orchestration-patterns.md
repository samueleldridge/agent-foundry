# 30 — Orchestration Patterns

## Purpose

Orchestration is how agents are wired together into a runnable multi-agent system. This doc specifies the **pattern library** — the fixed vocabulary of orchestration shapes the foundry supports. Each pattern defines: how nodes are connected, how state flows between them, how routing decisions are made, and how termination is reached.

The patterns themselves are **library code**, not configuration. The meta-agent picks among them; users compose them via YAML; what they compile to (LangGraph subgraphs and edges) is implementation detail. Picking a pattern shape that the foundry supports is bounded — meta-agent cannot invent novel orchestration topologies, only configure the supplied ones.

The `FlowSpec` discriminated union schema is in `12-config-and-validation.md`. State visibility behaviour is in `22-state-management.md`. The `Agent` and `SystemSpec` types are in `10-core-framework.md` and `01-architecture-overview.md`. This doc is the consolidating spec.

Two load-bearing properties:

1. **The pattern set is closed in v1.** `single`, `sequential`, `parallel`, `supervisor`, `graph`. Adding a new pattern requires a code change + tests + lint update — not a YAML edit. This is a deliberate safety property: meta-agent-generated configs cannot escape the pattern vocabulary.
2. **Patterns nest.** A supervisor's worker can be a sub-system with its own pattern. The compiler handles recursively. Composition is how complex shapes emerge without expanding the pattern vocabulary.

## Module layout

```
src/foundry/orchestration/
├── compiler.py         SystemSpec → CompiledSystem (the entry point)
├── patterns.py         five pattern compilers; each takes a sub-FlowSpec + agents and returns a sub-graph
├── state_scope.py      compile-time visibility validation
├── hitl.py             interrupt/resume semantics (Tier 3 → 32-human-in-the-loop.md)
└── handoff.py          handoff-tool generation for supervisor pattern

src/foundry/runtime/
└── langgraph_adapter.py   the only place LangGraph is touched
```

## Nodes: agents and function nodes

Every "step" referenced by a flow pattern is a **node**. The foundry has two node kinds:

- **Agent** — LLM-driven; full prompt + tools + memory + output-schema apparatus. See `21-agent-system.md`.
- **FunctionNode** — deterministic Python; no LLM, no tools, no prompt. Same flow position as an agent. See `21-agent-system.md` § Function nodes (and `10-core-framework.md` § FunctionNode protocol).

All five patterns below accept either kind interchangeably. A `sequential` flow can mix `[normalise_function_node, classifier_agent, format_function_node]`. A `supervisor`'s workers can be a mix. A `graph`'s `from`/`to` references resolve to either via the project's `agents:` or `functions:` lists in `SystemSpec`.

The compiler validates every node reference at compile time — every `to:` / `worker:` / `step:` must resolve to either an agent or a function node, and the resolution is unambiguous (names cannot collide across the two namespaces; the loader fails on collision).

## The five patterns

### 1. `single`

```yaml
flow:
  type: single
  agent: hello_agent
```

**Mental model**: one agent, one invocation. The simplest possible system.

**Compile**: a `StateGraph` with `START → <agent_node> → END`.

**State**: agent's `read` projection from initial state; `write` merged back to final state.

**Use cases**: development smoke tests, one-shot transformations (extract from this PDF), classifiers where the answer is the system's whole output.

**Termination**: returns when the agent's `BaseAgent.run()` returns or fails.

### 2. `sequential`

```yaml
flow:
  type: sequential
  steps: [classifier, investigator, recommender]
```

**Mental model**: a strict pipeline. Each agent runs to completion; its output state goes to the next agent's input.

**Compile**: `START → classifier → investigator → recommender → END`. Linear edges, no conditionals.

**State**: each agent reads its declared `read` fields and writes to its `write` fields. State accumulates as the pipeline progresses.

**Use cases**: deterministic ETL-shaped workflows, multi-step extraction (parse → enrich → format), staged review (draft → critique → finalise).

**Termination**: pipeline's last agent returns.

### 3. `parallel`

```yaml
flow:
  type: parallel
  parallel_branches: [classifier_a, classifier_b, classifier_c]
  join: aggregator
  then: [final_summary]
```

**Mental model**: fan-out + fan-in. Multiple agents run concurrently; their outputs converge at a join node; optional sequential continuation after the join.

**Compile**: under the hood uses LangGraph's `Send` API. Each branch is an independent task in an `anyio.create_task_group`; the join node waits for all branches.

**State**: each parallel branch sees the same input state (all branches see the same `read` fields). Outputs merge via reducers — the reducer choice is critical here. Two branches writing to the same `last_write_wins` field will produce a serialisation-order winner; better to use `APPEND` for parallel writes to lists or `MERGE` for namespaced dicts.

**Use cases**: ensembling (three classifiers vote), independent enrichments (look up trade + look up SSI + look up amendments concurrently), broad search (query 5 sources in parallel).

**Termination**: all branches complete + join completes + sequential `then` completes.

**Failure modes**:
- One branch fails (default: `cancel_siblings`) → other branches cancel cleanly via task group; join receives partial state with the failure recorded.
- One branch fails (`failure_mode: collect_all`) → other branches run to completion; join receives all results including failures recorded as state fields.

### 4. `supervisor`

```yaml
flow:
  type: supervisor
  supervisor: orchestrator
  workers: [break_detector, root_cause_investigator, resolver]

  handoff_policy:
    mode: llm                        # or 'rule'
    force_return_to_supervisor: true
    allowed_handoffs:
      orchestrator: [break_detector, root_cause_investigator, resolver, END]
      break_detector: [orchestrator]
      root_cause_investigator: [orchestrator]
      resolver: [orchestrator, END]

  termination:
    when: "state.resolution_complete == true"
    max_hops: 10
    on_max_hops: escalate            # 'escalate' | 'error' | 'return_partial'
```

**Mental model**: a supervisor agent decides which worker runs next. The loop continues until a termination condition fires. Workers can return to the supervisor (the typical pattern) or directly transition to other workers (rare; controlled by `allowed_handoffs`).

**Compile**: handoff decisions become **typed handoff tools** generated by the compiler. The supervisor agent receives `transfer_to_break_detector(reason: str)`, `transfer_to_root_cause_investigator(reason: str)`, etc. as tools (in addition to its own declared `tools`). When the supervisor calls `transfer_to_X`, the compiler routes the next node to `X`.

If `force_return_to_supervisor: true`, every worker's outgoing edge points back to `orchestrator` regardless of the worker's response (excluding the case where a worker is allowed to terminate via END).

**State**: supervisor's `read` and `write` typically cover most of state. Workers have narrower visibility per their specs. Subgraph projections still apply per the standard state-scope mechanism.

**Use cases**: investigation pipelines where the path isn't fixed, multi-tool-use scenarios where the orchestrator decides what's needed, branching escalation flows.

**Handoff modes**:
- **`llm`** (default): the supervisor LLM decides via tool call. Most flexible, non-deterministic.
- **`rule`**: routing is determined by a state predicate. The compiler evaluates the predicate after each worker; routes accordingly. Deterministic, easier to test, but inflexible.

**Termination**:
- `when:` is a Python expression evaluated against state. True → END.
- `max_hops:` caps the supervisor⇄worker round-trip count.
- `on_max_hops:` defines behaviour at cap: `error` raises `MaxHopsExceededError`, `return_partial` ends with current state, `escalate` requires a separate "escalation" worker named in config and forces a final handoff to it.

### 5. `graph`

```yaml
flow:
  type: graph
  start: triage
  edges:
    - from: triage
      to: low_severity_handler
      when: "state.severity == 'low'"
    - from: triage
      to: high_severity_investigator
      when: "state.severity in ['medium', 'high']"
    - from: low_severity_handler
      to: END
    - from: high_severity_investigator
      to: human_approver
      when: "state.confidence < 0.7"
    - from: high_severity_investigator
      to: auto_resolver
      when: "state.confidence >= 0.7"
    - from: auto_resolver
      to: END
    - from: human_approver
      to: auto_resolver
      when: "state.approved == true"
    - from: human_approver
      to: END
      when: "state.approved == false"
```

**Mental model**: a fully declared directed graph with conditional edges. Each agent is a node; each edge is a transition. Routing is deterministic — predicates are evaluated against state, no LLM in the routing path (use the supervisor pattern for LLM-driven routing).

**Compile**: each `from`/`to`/`when` becomes a LangGraph conditional edge. The `when:` predicate is a restricted Python expression evaluated against the current state via a sandboxed `eval` (allowed: comparisons, `and`/`or`/`not`, attribute access on state, `in` checks, common literals; forbidden: function calls, imports, anything stateful).

**State**: each node has its `read`/`write` projection per its `AgentSpec.state_visibility`.

**Use cases**: well-understood workflows with clear branching logic, compliance-sensitive flows where routing must be deterministic and auditable, escalation trees with explicit thresholds.

**Termination**: any edge to `END`.

**Validation**:
- Every named node exists in `SystemSpec.agents` (or is `END` / `START`).
- The graph is connected: every node is reachable from `start`; every node has a path to `END`.
- No cycles unless `cycles_allowed: true` (off by default; cycles risk infinite loops without explicit `max_hops`).
- All edges from a node form a complete cover (their `when:` predicates are mutually exclusive AND collectively exhaustive). Compile fails on holes.

## Composition (nesting)

Patterns nest. A supervisor's worker can itself be a sub-flow declared inline:

```yaml
flow:
  type: supervisor
  supervisor: orchestrator
  workers:
    - break_detector
    - investigation_subflow:
        type: parallel
        parallel_branches: [trade_lookup, ssi_check, amendments_check]
        join: aggregate_evidence
    - resolver
  handoff_policy: ...
```

The compiler handles recursively: each nested flow becomes a LangGraph subgraph that the parent flow treats as a single node. State scoping nests too — the parallel sub-flow's branches see the supervisor's view of state (per the supervisor's allowed `read`/`write`), then their own state-scope projections apply within.

Restrictions on nesting:
- A `single` flow can be nested anywhere as a degenerate case.
- A `sequential` flow can nest inside any other.
- A `parallel` flow's branches can be nested flows.
- A `supervisor`'s workers can be nested flows.
- A `graph`'s nodes can be nested flows. (Edge `to:` references the sub-flow's name.)
- A pattern cannot reference itself (no recursive nesting); detected at compile via a cycle check on the flow tree.

Nesting depth is configurable but capped at 4 by default (`SystemSpec.guardrails.max_flow_nesting_depth`). Deeper structures suggest the system should be split into multiple projects.

## Compile pipeline

`foundry.orchestration.compiler.compile(system_spec) → CompiledSystem`:

```
SystemSpec
   │
   ├── load + validate StateSpec (per 22-state-management.md)
   ├── load + validate every AgentSpec (per 21-agent-system.md)
   ├── load + validate every ToolSpec referenced (per 20-tool-system.md)
   ├── load + validate every ConnectionBinding (per 23-connections-and-auth.md)
   │
   ├── state_scope.validate(spec) → StateVisibilityError on hole
   │
   ├── walk FlowSpec recursively:
   │     ├── for each pattern: patterns.compile(<sub-spec>, agent_registry, state_compiler) → sub-graph
   │     ├── handoff_tool generation for supervisor patterns
   │     ├── conditional-edge predicate compilation for graph patterns
   │     └── subgraph wiring with state projections
   │
   ├── runtime/langgraph_adapter wraps everything in StateGraph
   ├── attach checkpointer (per Tier 7 selection)
   ├── attach RunEvent emitter wrapper at every node + edge transition
   │
   ▼
CompiledSystem
```

The `CompiledSystem` is what `foundry.api` and the CLI hold for execution. It carries:

- `state_graph: StateGraph` (LangGraph compiled object).
- `system_version: str` (content hash of the SystemSpec at compile time).
- `pin_set_hash: str` (hash of all resolved version pins).
- `agent_registry: dict[str, BaseAgent]`.
- `tool_registry: ToolRegistry`.
- `connection_pool: ConnectionPool`.

## Termination semantics per pattern

| Pattern | Natural termination | Override |
|---|---|---|
| `single` | agent returns | none — always one invocation |
| `sequential` | last step returns | early exit via state-predicate edge to END (would require switching to graph) |
| `parallel` | all branches join + then completes | `failure_mode: cancel_siblings` aborts on first failure |
| `supervisor` | supervisor invokes END handoff OR `termination.when` true OR `max_hops` exceeded | `on_max_hops` policy |
| `graph` | edge to END fires | cycles allowed only with explicit `max_hops` cap |

Every pattern respects the project-level `Guardrails`:
- `max_iterations` — total agent invocations across the whole run.
- `max_hops` — total edge traversals (a supervisor⇄worker round trip is 2 hops).
- `max_cost_usd` — `Session.cost_budget` enforced cumulatively.
- `max_wall_time_s` — wall clock; if exceeded, run cancelled with `RunCancelled(reason="timeout")`.

Whichever guardrail trips first ends the run. Guardrails are the safety net that prevents pathological flow configurations from consuming unbounded resources.

## Predicate language for `when:`

Used by `graph.edges[].when` and `supervisor.termination.when`. Restricted Python evaluated against a state proxy.

**Allowed**:
- Comparisons: `==`, `!=`, `<`, `<=`, `>`, `>=`, `in`, `not in`.
- Boolean: `and`, `or`, `not`.
- Attribute access: `state.field_name`, `state.field.subfield`.
- Subscript: `state.list[0]`, `state.dict['key']`.
- Literals: numbers, strings, booleans, `None`.
- Built-in checks: `len(state.field) > N`, `isinstance(state.field, str)`.

**Forbidden**:
- Function calls except whitelisted (`len`, `isinstance`, `bool`, `str`, `int`, `float`).
- Imports.
- Lambdas.
- Comprehensions.
- Mutation.
- Any non-pure expression.

Implementation: a small AST validator + sandboxed `eval` with restricted globals. Predicates are validated at compile (parse + AST walk for forbidden constructs) and evaluated at runtime against the state projection.

If you need a complex predicate, write a router agent (a single-LLM-call agent that emits a routing decision as its output). Cleaner to test and audit than escalating predicate-language complexity.

## Handoff tool generation (supervisor pattern only)

The compiler generates handoff tools for the supervisor based on its `allowed_handoffs[supervisor_name]` list. Each handoff target produces a tool with shape:

```python
class TransferToWorkerInput(BaseModel):
    reason: str = Field(min_length=10, description="Why this worker is being invoked.")

class TransferToWorkerOutput(BaseModel):
    handoff_recorded: bool = True   # always true; LLM uses output as confirmation
```

Tool name: `transfer_to_<worker_name>`. Description (auto-generated from worker's `description` field):

```
Hand off the current investigation to <worker_name>. <worker_description>
Use this tool when:
- (filled by the meta-agent or human in the supervisor's prompt)
```

When the supervisor LLM calls `transfer_to_<worker>`, the dispatcher:
1. Records the handoff event (`foundry.handoff` with from/to/trigger=`llm`/hop_number).
2. Returns success to the supervisor's response.
3. Routes the next node to `<worker>`.

Handoff tools are NOT in `agent.tools` allowlist — they're synthesised by the compiler and added to the supervisor's tool list at compile time. Handoff tools have a `dangerous: false` baseline and respect the same allowlisting as user tools (a handoff to an unallowed worker would have failed at compile time).

### Handoff to `END`

The compiler also synthesises a `transfer_to_end(reason: str)` tool when `END` is in the supervisor's `allowed_handoffs`. Calling it terminates the supervisor pattern's run.

### `mode: rule` (no LLM in routing path)

When `handoff_policy.mode: rule`, no handoff tools are generated. Instead, after each worker completes, the runtime evaluates a state predicate per the `allowed_handoffs[worker_name]` list:

```yaml
allowed_handoffs:
  break_detector:
    - to: investigator
      when: "len(state.detected_breaks) > 0"
    - to: END
      when: "len(state.detected_breaks) == 0"
```

Same predicate language. Same compile-time validation. Determinism is the trade for flexibility.

## State projection nuances

Each pattern interacts with state visibility:

- **`single`**: trivial. Agent's `read`/`write` apply.
- **`sequential`**: each step's `read`/`write` projection applies. State accumulates.
- **`parallel`**: each branch sees the *same* input state (each agent's `read` projection). Outputs from concurrent branches merge via reducers — choose reducers carefully for fields written by multiple branches.
- **`supervisor`**: supervisor's `read`/`write` sees the full pipeline view (typically broad). Workers have their own narrower projections.
- **`graph`**: each node's projection applies independently per its agent's spec.

State visibility is enforced before each agent invocation regardless of pattern. The compiler validates that every `to:` reference in a graph or `worker:` in a supervisor maps to an agent with valid visibility.

## Observability per pattern

Patterns affect what shows up in the audit stream:

- **`single`**: one `agent.started` + one `agent.completed`.
- **`sequential`**: N pairs of agent.started/completed in order.
- **`parallel`**: N pairs concurrently (may interleave by completion order); `state.transition` events emitted in order of write.
- **`supervisor`**: supervisor + worker `agent.*` events + `handoff` events for every transition. Heavy event stream — useful for debugging routing.
- **`graph`**: agent.* events plus `handoff` events with `trigger: rule` for each conditional traversal (carries the predicate that fired).

The `RunEvent.Handoff` taxonomy already supports this. Tooling (`foundry obs`, dashboards) can filter by `trigger` to distinguish LLM-driven handoffs from rule-driven ones.

## Failure modes

| Cause | Surfaced as | Where caught |
|---|---|---|
| Unknown pattern type | `UnknownPatternError` | compiler |
| Graph node references unknown agent | `CompileError` | compiler |
| Graph edges reference unknown node | `CompileError` | compiler |
| Graph has unreachable node | `CompileError` (warning if `cycles_allowed`, else error) | compiler |
| Supervisor handoff to unallowed worker | `CompileError` | compiler |
| Predicate fails AST validation | `CompileError` with the violating syntax | compiler |
| `max_hops` exceeded | `MaxHopsExceededError` | runtime; behaviour per `on_max_hops` |
| `max_iterations` exceeded | `OrchestrationError` (subclass `IterationLimitError`) | runtime |
| Predicate raises at runtime (state field missing) | `OrchestrationError` with predicate text | runtime |
| Parallel branch fails (cancel_siblings) | propagates as `MultiBranchError` after sibling cancellation | runtime |
| Parallel branch fails (collect_all) | recorded in state field; aggregator handles | runtime |
| Nested flow circular | `CompileError("nested flow cycle")` | compiler |

## Parallel guard / observer pattern

A common need: a "guard" or "monitor" that evaluates state continuously alongside the main flow without blocking it. Examples:
- **Compliance monitor** — checks every agent output for policy violations; cancels the run on breach.
- **Cost trajectory monitor** — watches cumulative spend; cancels if the run is trending toward budget breach before the hard cap fires.
- **Adversarial / prompt-injection detector** — scans tool outputs being injected back into the LLM for known attack patterns.
- **Quality drift monitor** — compares the run's behaviour to a baseline; flags anomalies for human review post-hoc.

The foundry does NOT have this as a flow primitive. It's a configuration shape that doesn't fit cleanly under any of the five patterns (it's not parallel-with-join, it's not a graph node, it's not a supervisor's worker — it's a side-channel observer).

Three mechanisms cover the use cases. Pick by latency / blocking tolerance.

### Mechanism 1: `LifecycleHooks` (synchronous, blocking)

`BaseAgent.LifecycleHooks` (per `10-core-framework.md`) fires `before_node` / `after_node` / `on_error` / `before_tool` / `after_tool` synchronously around every node and tool. Good for fast, blocking checks where the result must influence the next step.

```python
async def output_pii_scanner(agent, result, state, session) -> None:
    """after_node hook. Blocks the run until scan completes."""
    if contains_pii(result.output):
        session.cancel_token.cancel(reason="pii_in_output")

hooks = LifecycleHooks(after_node=output_pii_scanner)
```

Tradeoffs:
- ✅ Simple. Already supported. Cancellation propagates cleanly.
- ❌ Blocks the run. A 500ms compliance LLM-judge call adds 500ms to every node.
- ❌ One project = one hooks bundle (per agent). Multiple guards compose by the hooks themselves, but it gets messy.

Use when: the check is fast (<50ms) and its result must gate the next step.

### Mechanism 2: Event-stream observer (async, non-blocking) — RECOMMENDED for most guards

Every run publishes a typed `RunEvent` stream (`10-core-framework.md` § Streaming events). An external async task can subscribe to the stream, evaluate events asynchronously, and call `session.cancel_token.cancel(reason)` when it detects a violation. The main flow never waits on the observer.

```python
async def compliance_guard(run_id: RunId, session: Session, judge: Provider):
    """Subscribes to the run's event stream. Runs an LLM-judge on every
    agent.completed event in parallel with the main flow. Cancels on
    policy violation."""
    async for event in session.event_stream():
        if event.event == "agent.completed":
            verdict = await judge.generate(
                messages=build_compliance_prompt(event),
                tools=[],
                settings=ModelSettings(temperature=0, max_tokens=200),
            )
            if violates_policy(verdict):
                session.cancel_token.cancel(reason=f"compliance_violation: {verdict.summary}")
                # Persist the finding for audit:
                await session.observability.write_guard_finding(
                    run_id=run_id,
                    guard="compliance",
                    event=event,
                    verdict=verdict,
                )
                return  # observer terminates after firing
```

The observer is launched as a sibling task when the run starts:

```python
async with anyio.create_task_group() as tg:
    tg.start_soon(compiled_system.run, input, session=session)
    tg.start_soon(compliance_guard, session.run_id, session, judge_provider)
```

Tradeoffs:
- ✅ Non-blocking. Main flow runs at full speed; guard runs in parallel.
- ✅ Multiple guards compose by adding more sibling tasks.
- ✅ Guard logic lives in project code; testable independently.
- ⚠️ Race condition: a violation may be detected after the offending output has already been used (e.g., emitted to the LLM in the next turn). Mitigation: pair with `after_node` lifecycle hook for the strictly-must-block-before-next-node case.

Use when: guard latency is non-trivial (LLM judges, slow heuristics) and the violation can be acted on with eventual consistency (cancel the run before final output, even if some intermediate state was emitted).

### Mechanism 3: Hybrid (observer + critical-path hook)

For maximum coverage, combine both:

```python
# Fast check on critical-path output (blocks if violation):
def critical_path_check(agent, result, state, session):
    if obvious_violation(result.output):
        session.cancel_token.cancel(reason="obvious_violation")

# Slower nuanced check via observer (catches what fast check misses):
async def nuanced_observer(...):
    # LLM-judge on every event...
```

The fast check catches obvious violations cheaply with blocking certainty; the observer catches nuanced violations without slowing the main path.

### Why not a built-in flow primitive

A `guard` flow primitive was considered. Rejected for v1:

- The two existing mechanisms (lifecycle hooks + event-stream observers) cover the use cases without adding pattern surface.
- A flow primitive would need to specify when the guard runs (before/after every node? every event?), which already overlaps with hooks + observers.
- Project-side composition via `anyio.create_task_group` is ~10 lines of code; a primitive would add YAML config that compiles to roughly the same thing.

If 3+ projects build similar guards from the same template, promote the template to `catalog/guard_templates/` (analogous to `agent_templates/`). v1.1 work.

### Audit completeness

Guard findings (regardless of mechanism) MUST be written to the audit store via `session.observability.write_guard_finding(...)`. This is a required call so the audit trail records "guard X fired on run Y because of event Z." Querying audit for guard fires:

```bash
$ foundry obs guards --project pipeline_recon --since 7d
```

Implementation lives in `foundry.observability.guards`; the API is intentionally narrow so guards can't pollute the audit store with arbitrary writes.

## Custom patterns (deferred)

The pattern set is fixed in v1. If a real workflow can't be expressed via the five patterns + nesting, the right answer is *probably* a graph (which is fully general for static topologies) or a supervisor (which is fully general for LLM-routed topologies).

Custom-pattern primitive (`Pattern` protocol that users implement) is **not** in v1. Risks:
- A wrong-shape custom pattern bypasses state-scope enforcement.
- Meta-agent prompt would have to know about user-defined patterns; complexity blow-up.

Documented design vector for v1.1: a `CustomPattern` protocol with mandatory state-scope hooks; reviewed via PR for promotion to the catalog. Defer until 3+ projects need it.

## Invariants

1. **Pattern set is closed at runtime.** Adding a pattern requires a code change in `foundry.orchestration.patterns`; meta-agent cannot synthesise new patterns.
2. **State scope is enforced regardless of pattern.** Every node's projection respects its agent's `state_visibility` declaration.
3. **Every transition emits a `handoff` event** (except `single` which has no transitions). Audit completeness is non-negotiable.
4. **Predicates are pure.** No side effects; no mutation; no I/O. Sandboxed AST validation at compile.
5. **Guardrails always win.** `max_hops` / `max_iterations` / `max_cost_usd` / `max_wall_time_s` cap every pattern.
6. **Handoff tools are compile-generated, not user-authored.** Users cannot register their own handoff tools.
7. **Nested flows are projection-aware.** A sub-flow inherits its parent's state view, then applies its own scope.
8. **`END` is a sink.** No node has `END` as a `from:`.

## Test expectations

### Unit

1. **Pattern compilation**: each of the five patterns compiles a minimal example to a runnable graph; smoke-test runs against a fake provider end-to-end.
2. **Predicate AST validator**: forbidden constructs (function calls, imports, comprehensions) raise `CompileError` with line/column.
3. **Predicate evaluation**: `state.severity in ['low', 'medium']` evaluates correctly against a state with that field; missing field → `OrchestrationError`.
4. **Graph reachability check**: a graph with an unreachable node fails compile with the node name.
5. **Graph completeness check**: a node with edges that don't cover all states (e.g. missing `else`) fails compile unless `cycles_allowed: true`.
6. **Handoff tool generation**: a supervisor with workers `[a, b]` produces tools `transfer_to_a` and `transfer_to_b` with auto-generated descriptions.
7. **Handoff to disallowed worker**: supervisor calls `transfer_to_c` (not in `allowed_handoffs[supervisor]`) → `ToolNotFoundError` (compile-time guarantee — the tool doesn't exist for the supervisor).
8. **`force_return_to_supervisor`**: worker's outgoing edge always points to supervisor regardless of worker's response.
9. **Parallel reducer correctness**: 3 branches all writing to `messages: APPEND` produce concatenation; 3 branches writing to `final: LAST_WRITE_WINS` produce a serialisation-order winner.
10. **Nesting**: a supervisor whose worker is a parallel sub-flow compiles and runs end-to-end.

### Contract

1. **Pattern set is closed**: a config with `flow.type: my_custom_pattern` fails with a clear `UnknownPatternError` enumerating the valid types.
2. **State visibility enforced across patterns**: an agent in a graph node attempting to read a forbidden field fails at compile (state-scope check is pattern-agnostic).
3. **Handoff events complete**: for any multi-node pattern, `foundry.handoff` event count = number of edge traversals.

### Integration (Phase 7 exit gate, see `03-development-phases.md`)

1. End-to-end supervisor-with-3-workers: scoped state per worker; handoff events visible in audit; `force_return_to_supervisor: true` honoured.
2. End-to-end graph with conditional routing: predicates evaluate correctly; trace shows the exact route taken.
3. End-to-end parallel fan-out + fan-in: 3 branches concurrent, aggregator joins, sequential `then` runs.
4. `max_hops` enforcement: contrived loop hits cap; behaviour per `on_max_hops` (test all three modes).

## Open questions

1. **Sub-graph-as-agent abstraction.** A composed `parallel(...)` could be addressable as a single named "agent" elsewhere in the config. Useful for reuse. Lean: defer; nesting via inline definition covers the common case.
2. **Streaming partial outputs across pattern boundaries.** Currently parallel branches' outputs land at the join all at once. Some use cases want per-branch progressive streaming to clients. Lean: defer; existing `RunEvent` stream already shows per-branch events.
3. **Pattern-aware retries.** Currently retries are per-LLM-call (provider) and per-tool (tool). Should there be pattern-level retries (e.g. "retry the whole supervisor flow if it returns low confidence")? Probably better expressed as an outer-supervisor pattern; defer.
4. **Predicate-language extension for cost budgets.** `when: state.cost_so_far_usd < 0.50` would make cost-aware routing possible. Already supported (state field is accessible). Document as a pattern, not a primitive.
5. **Visual flow editor.** Out of scope for v1. The `foundry obs` CLI + a graph-rendering capability for `foundry serve`'s `/config` endpoint is sufficient for understanding deployed systems.
