# 31 — Multi-Agent Systems

## Purpose

A multi-agent system is the foundry's deployable unit — a `SystemSpec` + its agents + state + tool/connection bindings + flow + guardrails, all compiled into something runnable. This doc consolidates the system-level surface: how a system is declared, how compilation works, how runs are launched, how external callers interact (input/output contracts), and how API endpoints get auto-generated from the spec.

`SystemSpec` schema is in `12-config-and-validation.md`. Patterns are in `30-orchestration-patterns.md`. State is in `22-state-management.md`. The compile pipeline is sketched in `30`. This doc is the consolidating spec at the system level.

Three load-bearing properties:

1. **A system is fully described by its `SystemSpec` + the files it references.** No code beyond what the agents themselves contain. The compiler consumes the YAML tree and produces a `CompiledSystem` ready to run.
2. **External callers see a typed API**. The auto-generated FastAPI surface (`70-api-layer.md`) derives request/response shapes from the project's input schema and the terminal agent's output schema; OpenAPI is free.
3. **Systems are reproducible.** Same `system_version` (git sha + pin-set hash) + same input + same seed → same outputs (modulo provider non-determinism, which is bounded by `temperature: 0` + the eval harness's tolerance).

## What a multi-agent system IS

| IS | IS NOT |
|---|---|
| The unit of `foundry serve` and `foundry run` | A unit of agent reuse (agents are project-scoped) |
| The unit of versioning + rollback | The unit of network deployment (multiple systems may share one `foundry serve` process) |
| Owner of state, flow, guardrails | Owner of agents (agents own themselves; system references them) |
| Source of API endpoint generation | Source of UI generation (no UI generation in v1) |
| Eval target at the project level | Eval target at agent / tool level (those have their own evals) |

## Project layout (recap from `01-architecture-overview.md`)

```
projects/<project_name>/
├── system.yaml             SystemSpec — the manifest
├── state.yaml              StateSpec — fields, reducers, visibility
├── agents/
│   └── <agent_name>/
│       ├── agent.yaml      AgentSpec
│       ├── prompts/v<N>.md
│       └── output_schema.py
├── tools/                  project-local tools (optional)
│   └── <tool_name>/v<N>/...
├── connections/            project-local connections (optional)
│   └── <connection_name>/v<N>/...
├── evals/                  project-level eval sets (optional)
│   └── <eval_name>.yaml
└── .foundry/               per-project metadata (audit log)
```

`SystemSpec` is the entrypoint; everything else is referenced from it.

## Full `SystemSpec` walkthrough

The complete schema is in `12-config-and-validation.md`. A realistic example showing every field in use:

```yaml
name: pipeline_recon
description: |
  Investigate exception breaks during nightly reconciliation; classify root
  cause; auto-resolve under threshold or escalate.

agents: [orchestrator, break_detector, root_cause_investigator, resolver]

state: state.yaml

flow:
  type: supervisor
  supervisor: orchestrator
  workers: [break_detector, root_cause_investigator, resolver]
  handoff_policy:
    mode: llm
    force_return_to_supervisor: true
    allowed_handoffs:
      orchestrator: [break_detector, root_cause_investigator, resolver, END]
      break_detector: [orchestrator]
      root_cause_investigator: [orchestrator]
      resolver: [orchestrator, END]
  termination:
    when: "state.investigation_result is not None"
    max_hops: 12
    on_max_hops: escalate

tools:
  query_snowflake:
    ref: catalog/query_snowflake
    version: v2
    settings: {timeout_s: 45.0}
    connection_bindings:
      warehouse: prod_snowflake
  validate_deltas:
    ref: local/validate_deltas
    version: v3
    connection_bindings:
      reference_db: prod_snowflake
  send_slack:
    ref: catalog/send_slack
    version: v1
    connection_bindings:
      slack: ops_slack
  trigger_rpa:
    ref: catalog/trigger_rpa
    version: v1
    connection_bindings:
      rpa: prod_rpa

connections:
  prod_snowflake:
    ref: catalog/snowflake
    version: v2
    config:
      account: ${ENV:SNOWFLAKE_ACCOUNT}
      warehouse: RECON_WH
      role: RECON_INVESTIGATOR_RO
    credentials_ref:
      kind: secret_manager
      value: vault/ops/recon/snowflake
  ops_slack:
    ref: catalog/slack_workspace
    version: v1
    config:
      workspace: ops-prod
      default_channel: "#recon-alerts"
    credentials_ref:
      kind: secret_manager
      value: vault/ops/recon/slack
  prod_rpa:
    ref: catalog/rpa_uipath
    version: v1
    config: {orchestrator_url: https://uipath.internal/orchestrator}
    credentials_ref:
      kind: secret_manager
      value: vault/ops/recon/uipath

guardrails:
  max_iterations: 30
  max_hops: 15
  max_cost_usd: 5.00
  max_wall_time_s: 600

observability:
  trace: otel
  sample_rate: 1.0
  capture_inputs: true
  capture_outputs: true
  capture_tool_args: true

metadata:
  owner: ops-team
  cost_category: nightly_recon
  on_call_runbook: https://runbooks.internal/recon

schema_version: 1
```

Every field is consumed by the compiler. Nothing is decorative.

## The compile pipeline (full)

`foundry.orchestration.compiler.compile(project_root: Path) → CompiledSystem`:

```
┌─────────────────────────────────────────────────────────────┐
│  1. LOAD                                                    │
│     - system.yaml → SystemSpec                              │
│     - state.yaml → StateSpec                                │
│     - each agents/<name>/agent.yaml → AgentSpec             │
│     - each pinned tool's tool.yaml → ToolSpec               │
│     - each pinned connection's connection.yaml → ConnSpec   │
│     - each retriever template's retriever.yaml → RSpec      │
└─────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  2. RESOLVE                                                 │
│     - ArtifactRef resolution against FoundryRoots           │
│     - prompt file content for each agent's pinned version   │
│     - output_schema.py imports + class lookups              │
│     - tool handler.py imports + class lookups               │
│     - connection auth.py imports + factory lookups          │
│     - retriever factory.py imports                          │
│     - secrets via SecretsProvider per credentials_ref       │
└─────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  3. VALIDATE (compile-time)                                 │
│     - StateSpec field types parse                           │
│     - state visibility coverage (every agent has entry)     │
│     - state visibility references valid fields              │
│     - tool allowlist references valid SystemSpec.tools keys │
│     - tool connection slots match ToolBinding bindings      │
│     - connection config matches ConnectionSpec config_schema│
│     - capability-required: provider+model supports          │
│     - capability-required: embedder dimensions match cache  │
│     - flow.type valid + flow references valid agents        │
│     - flow predicates AST-valid                             │
│     - graph reachability (every node reachable, all to END) │
│     - secret-literal scan over all configs                  │
│     Failures: ConfigError / ValidationError / VisibilityError│
└─────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  4. INSTANTIATE                                             │
│     - StateCompiler.compile(StateSpec) → CompiledState      │
│       (Pydantic state model + per-agent TypedDicts +        │
│        reducer dict)                                        │
│     - For each AgentSpec:                                   │
│         compile to a BaseAgent instance                     │
│         (Provider resolved, tools wired, retrievers wired,  │
│          memory wired, semantic_cache wired, hooks attached)│
│     - For each ToolSpec: register in ToolRegistry           │
│     - For each ConnectionBinding: register in ConnectionPool│
│     - Handoff tools generated for supervisor patterns       │
│     - Generate per-agent input/output projection wrappers   │
└─────────────────────────────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│  5. WIRE (LangGraph adapter)                                │
│     - Build StateGraph with state_schema=ProjectState       │
│     - Add nodes (one per agent or per nested sub-flow)      │
│     - Add edges per FlowSpec pattern compiler output        │
│     - Attach checkpointer (per Tier 7 selection)            │
│     - Attach RunEvent emitter wrapper at every node entry   │
│       and edge transition                                   │
│     - Attach observability span hooks                       │
│     - Compute system_version (sha of merged config) +       │
│       pin_set_hash                                          │
└─────────────────────────────────────────────────────────────┘
                         │
                         ▼
                  CompiledSystem
```

Compile is **deterministic**: same input files + same FoundryRoots = same `CompiledSystem` (modulo `system_version` which is itself derived from the input). This is what makes systems reproducible.

## `CompiledSystem`

```python
class CompiledSystem(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    name: str
    system_version: str       # content hash of full SystemSpec + pinned files
    pin_set_hash: str         # hash of resolved version pins
    project_root: Path
    framework_version: str    # foundry package version

    state: CompiledState
    agents: dict[str, BaseAgent]
    tool_registry: ToolRegistry
    connection_pool: ConnectionPool
    flow: CompiledFlow
    guardrails: Guardrails
    observability: ObservabilityConfig

    state_graph: Any          # LangGraph StateGraph; opaque outside the runtime adapter

    async def run(
        self,
        input: dict[str, Any],
        session: Session | None = None,
        run_id: RunId | None = None,
    ) -> RunResult: ...

    def astream(
        self,
        input: dict[str, Any],
        session: Session | None = None,
        run_id: RunId | None = None,
    ) -> AsyncIterator[RunEvent]: ...

    async def resume(
        self,
        run_id: RunId,
        inbound: InboundMessage | None = None,
    ) -> RunResult: ...
```

The `CompiledSystem` is what the API layer holds (one per running project; lifetime = serve process), what the CLI's `foundry run` constructs per invocation, and what the eval harness uses for cases.

`run()` and `astream()` are thin wrappers over the underlying LangGraph execution; the runtime adapter generates `RunEvent`s and dispatches them to the session's event sink.

## Run lifecycle (from caller's perspective)

### Non-streaming `run()`

```python
result = await compiled.run(input={"trade_id": "ABC123"})
# result: RunResult(run_id=..., output=<terminal_agent_output>, status="success", ...)
```

Sequence:
1. Validate `input` against the project's input schema (see § Input contract).
2. Construct or accept `Session` (with cost budget per `Guardrails.max_cost_usd`).
3. Mint or accept `RunId`.
4. Initialize state from `input` + defaults.
5. Run the `state_graph.ainvoke(initial_state, config)`.
6. Throughout: emit `RunEvent`s; honour `cancel_token`; checkpoint at each node.
7. On success: extract terminal output (the `output` field of the agent that's the natural "exit"); validate against the system's output schema; return `RunResult`.
8. On failure: emit `RunFailed`; return `RunResult` with status=`failed` + error context.

### Streaming `astream()`

```python
async for event in compiled.astream(input={"trade_id": "ABC123"}):
    # event is a RunEvent (RunStarted, LLMDelta, ToolCompleted, ..., RunCompleted)
    print(event)
```

Same execution; events emitted progressively. The `RunResult` is implicitly available via the terminal `RunCompleted` event's `final_output`.

### Resume `resume()`

For runs that were interrupted (process killed, HITL approval pending, etc.):

```python
# Run was interrupted at hop 5 awaiting approval
# Approval came in from operator; resume:
result = await compiled.resume(
    run_id=existing_run_id,
    inbound=ApprovalResponse(approval_id="...", decision="approved"),
)
```

Resume:
1. Load checkpointed state from the checkpointer using `run_id`.
2. Apply `inbound` (if provided): for `ApprovalResponse`, marks the pending tool/agent as approved; for `InjectInput`, appends a message; for `CancelRun`, terminates with `RunCancelled`.
3. Continue execution from the last checkpoint.
4. Emit `RunEvent`s as normal; the stream's `sequence` continues from the last persisted sequence (NOT 0).

Detail in `32-human-in-the-loop.md`.

## Input contract

A system has a typed input schema derived at compile time. Sources:

1. **Initial state population**: state fields without defaults that the run needs to start are part of the input. Fields with defaults (or `None`-defaulting Optional fields) are optional inputs.
2. **Convention**: a `__user_input__: true` field marker on `StateSpec.schema` (planned for v1.1) explicitly identifies the input shape. Until then: any state field with no default that's *read* by the start node (the supervisor in `supervisor` pattern, the first step in `sequential`, the `start` in `graph`, the agent in `single`) is part of the input.

The compiler computes the input schema as a Pydantic model:

```python
class PipelineReconInput(BaseModel):
    trade_id: str
    observed_mismatch_usd: float
    timestamp: datetime
```

This is what:
- The CLI's `foundry run --input '{...}'` validates against.
- The auto-generated `POST /run` endpoint accepts.
- The auto-generated OpenAPI schema declares.
- The eval harness's `EvalCase.input` validates against.

Invalid input → `ConfigValidationError` at the boundary; API returns `400 Bad Request` with field-level error detail.

## Output contract

A system has a typed output schema derived at compile time. Sources:

- For `single` pattern: the agent's `output_schema`.
- For `sequential`: the last step's `output_schema`.
- For `parallel`: the join node's `output_schema` (or, if no `then:`, the last branch's — controversial; recommend always specifying `join`).
- For `supervisor`: the supervisor's `output_schema` (the supervisor produces the final answer when handing off to END).
- For `graph`: the agent whose `to: END` edge was last traversed.

For multi-terminal patterns where the output type depends on which path was taken, the compiler may produce a discriminated union:

```python
PipelineReconOutput = Annotated[
    AutoResolvedOutput | EscalatedOutput | InvestigationFailedOutput,
    Field(discriminator="result_kind"),
]
```

This requires each terminal agent's `output_schema` to declare a `result_kind: Literal["..."]` field — the compiler infers the union discriminator from those literals. If discriminators conflict or are missing, compile fails with a clear error.

## Auto-generated API endpoints

`foundry serve <project>` instantiates a `FastAPI` app with endpoints generated from the `CompiledSystem`. Per `01-architecture-overview.md` § API summary:

| Endpoint | Source |
|---|---|
| `POST /run` | request: `<project_input_schema>`; response: `<project_output_schema>` |
| `POST /stream` | request: same; response: SSE stream of `RunEvent` |
| `POST /batch` | request: `list[<project_input_schema>]` + batch policy; response: SSE stream tagged with `batch_id` + `item_id` |
| `WS /ws` | bidirectional: outbound `RunEvent`, inbound `InboundMessage` |
| `GET /runs/{run_id}` | response: `RunStatus` (typed) |
| `GET /runs/{run_id}/events?from_sequence=N` | response: SSE replay |
| `POST /runs/{run_id}/resume` | request: `ApprovalResponse \| InjectInput \| CancelRun`; response: `RunResult` |
| `GET /health` | response: `Health` (per-connection healths aggregated) |
| `GET /config` | response: redacted compiled-config snapshot |

The OpenAPI schema at `/openapi.json` contains real Pydantic-derived shapes — clients can codegen typed bindings without writing schemas by hand.

Full wire format and auth pluggability in `70-api-layer.md`.

## How the system identity is computed

`system_version` is the project's content hash:

```python
system_version = sha256(
    canonical_json({
        "system_spec": load("system.yaml"),
        "state_spec": load("state.yaml"),
        "agents": {
            name: {
                "agent_spec": load(f"agents/{name}/agent.yaml"),
                "prompt": load(f"agents/{name}/prompts/v{pinned}.md"),
                "output_schema_source": load(f"agents/{name}/output_schema.py"),
            }
            for name in system_spec.agents
        },
        "tools": {
            tool_logical_name: load(resolve_artifact(binding))
            for tool_logical_name, binding in system_spec.tools.items()
        },
        "connections": {
            conn_logical_name: load(resolve_artifact(binding))
            for conn_logical_name, binding in system_spec.connections.items()
        },
        "framework_version": foundry.__version__,
    })
)[:16]
```

`pin_set_hash` is just the hash of the pin set (versions only, no file content):

```python
pin_set_hash = sha256(
    canonical_json({
        "tools": {n: f"{b.ref}@{b.version}" for n, b in system_spec.tools.items()},
        "connections": {n: f"{b.ref}@{b.version}" for n, b in system_spec.connections.items()},
        "agent_prompts": {n: agent.prompt.version for n, agent in agents.items()},
    })
)[:16]
```

`pin_set_hash` is what `foundry eval compare --pin-set` uses to identify configurations cheaply (without computing full file-content hashes).

Both are surfaced in `RunEvent.RunStarted` and stored on every run artifact. Trivial to query: "what version of pipeline_recon ran on this trade?"

## Cross-agent communication (recap)

Agents communicate exclusively through state. There are no direct agent-to-agent message passing primitives. The supervisor pattern's "handoff tools" are routing hints, not data passes — when supervisor invokes `transfer_to_worker(reason="...")`, the worker reads its declared `read` fields from the merged state, not the supervisor's reasoning text.

If a worker needs the supervisor's reasoning, the supervisor must write that reasoning to a state field the worker reads. Make it explicit:

```yaml
# state.yaml
schema:
  routing_reason:
    type: str | None
    default: null
visibility:
  orchestrator: { read: [...], write: [..., routing_reason] }
  break_detector: { read: [..., routing_reason], write: [...] }
```

The supervisor's prompt instructs it to write `routing_reason` before handing off. This is verbose but explicit; the framework doesn't auto-thread reasoning text because doing so would couple agents and degrade testability.

## Testing a system

Three layers of test cover a system:

| Test | Granularity | Doc |
|---|---|---|
| Per-tool standalone eval | one tool | `20-tool-system.md` |
| Per-agent eval | one agent against scoped state | `21-agent-system.md` |
| Project-level end-to-end eval | whole compiled system | `40-eval-harness.md` |

Plus framework-level integration tests:
- Compile a fixture system; assert `CompiledSystem.system_version` is deterministic.
- Run a fixture system against a fake provider; assert `RunResult` shape and event sequence.
- Bump a pin; assert `system_version` changes.
- Exercise `Guardrails.max_cost_usd` via a costly fixture; assert `RunFailed` with `CostBudgetExceeded`.

## Observability at system level

Every system run emits a `foundry.run` span (per attribute spec in `01-architecture-overview.md`). At system level, the most useful aggregates:

- **Cost per run**, by project, by day → `foundry obs cost --project <name>`.
- **End-to-end latency p50/p95** → `foundry obs latency --project <name>`.
- **Failure rate** + breakdown by `error_class` → `foundry obs failures --project <name>`.
- **Pin-set distribution** (which pin sets are running today) → `foundry obs pin-sets --project <name>`.

Per-system dashboards build on these with one-time setup; the audit stream provides the inputs.

## Failure modes

| Cause | Surfaced as | Where caught |
|---|---|---|
| `system.yaml` invalid | `ConfigValidationError` | loader |
| Referenced state spec missing | `ConfigLoadError` | loader |
| Agent in `agents` list lacks `agents/<name>/` directory | `ConfigError` | compiler |
| Agent's `model_binding` capability not supported | `ProviderConfigError` | compiler |
| Tool ref unresolvable | `RefResolutionError` | compiler |
| Connection ref unresolvable | `RefResolutionError` | compiler |
| State visibility hole | `StateVisibilityError` | compiler |
| Flow validation fails (unknown node, predicate AST, etc.) | `CompileError` / `OrchestrationError` | compiler |
| Input fails project input schema | `ConfigValidationError` (boundary), `400` (API) | run() / API |
| Run hits a guardrail | `OrchestrationError` subclass + `RunFailed` event | runtime |
| Provider auth fails | `ProviderAuthError` + `RunFailed` | runtime |
| Connection unavailable | `ConnectionError` subclass + (typically) `RunFailed` | runtime |
| Output fails project output schema | `OutputValidationError` | runtime (after auto-repair) |
| Caller cancels mid-run | `RunCancelled` + `RunCancelled` event | runtime |

Every failure mode produces a typed `RunEvent` for the audit trail; the API surfaces structured error JSON with `error_class`, `message`, `context`.

## Invariants

1. **`SystemSpec` is the single source of truth.** Compile is deterministic from the spec + referenced files.
2. **`system_version` is content-hashed.** Any change to spec, prompt, output schema, tool, or connection bumps it.
3. **`pin_set_hash` is the cheap key for cross-version comparison.** Used by eval-comparison workflows.
4. **Compile fails fast on any structural issue.** No "best-effort" runs against half-validated systems.
5. **Project input is typed and validated at the boundary.** No untyped dicts cross into the runtime.
6. **Project output is typed.** Multi-terminal flows produce a discriminated union; each terminal agent contributes a discriminator.
7. **Resume preserves identity.** A resumed run has the same `run_id` and `system_version` as the original; `pin_set_hash` is locked to compile-time, not runtime (rolling forward pins doesn't affect in-flight runs).
8. **One process can serve many systems.** `foundry serve` accepts multiple `--project` flags; each gets its own URL prefix (`/<project>/run`, `/<project>/stream`, ...).

## Test expectations

### Unit

1. **Compile determinism**: same files → same `CompiledSystem.system_version`.
2. **`pin_set_hash` independence**: changing a prompt content (without a pin bump) changes `system_version` but NOT `pin_set_hash`.
3. **Input schema generation**: a project with three agents and four state fields with no defaults produces an input schema containing exactly those four fields.
4. **Output schema generation**: a single-pattern project's output schema equals the agent's `output_schema`.
5. **Multi-terminal output union**: a graph with two `to: END` edges from two different agents produces a discriminated union output.
6. **Validation cascade**: a project with an unresolvable connection ref fails compile before flow validation runs (fail-fast).
7. **Guardrail propagation**: `Guardrails.max_cost_usd` is propagated to `Session.cost_budget` at run start.

### Contract

1. **OpenAPI schema correctness**: generated schema validates against draft 2020-12; `POST /run` request body matches input schema; response matches output schema (or union).
2. **Resume identity**: a run interrupted then resumed has identical `run_id` and `system_version` in both `RunStarted` events.

### Integration (Phase 7 / Phase 8 exit gates)

1. End-to-end: a hello-world project compiles, runs, returns expected output via CLI and via auto-generated `POST /run`.
2. Multi-agent supervisor system runs end-to-end; events reflect the full handoff stream.
3. `foundry serve --project a --project b` launches a single process serving two projects with correctly namespaced endpoints.
4. Bumping a tool pin, recompiling, and running shows changed `pin_set_hash` and `system_version`.

## Open questions

1. **Multi-system processes.** Default in v1: one project per `foundry serve`. Should multi-project serving be standard from day 1? Lean: single-project default; multi-project as `--project foo --project bar` opt-in.
2. **Project input from a separate schema file.** Currently inferred from state. A dedicated `project_input.py` Pydantic class would be more explicit. Lean: defer until the inferred shape proves insufficient; convention beats configuration here.
3. **System-level lifecycle hooks.** Project-wide `before_run` / `after_run` hooks (analogous to agent-level hooks). Useful for cross-cutting concerns (e.g., billing tracker that records every run to an external system). Lean: yes, additive — extend `Session` with project-level hook plumbing in Phase 7.
4. **Compile cache.** Compile is deterministic and not super cheap (resolves files, imports modules, hashes content). For large projects in dev, repeated compiles could benefit from a cache keyed on `(file_mtimes, framework_version)`. Lean: yes, in-process cache; per-run invalidation on file change. Phase 8 polish.
5. **System cloning.** `foundry project clone <source> <target>` to fork a project with a new name. Useful for A/B experiments. Lean: yes, simple CLI command; ship in Phase 5 alongside catalog promotion.
