# 21 — Agent System

## Purpose

An agent is the smallest behavioural unit in a foundry project: a model binding + a prompt + a typed output + an allowlist of tools, optionally with retrievers, memory, semantic cache, and per-node state visibility. This doc consolidates the full agent surface — schema, lifecycle, prompt assembly, output validation, iteration semantics, archetypes, scaffolding, and per-agent eval.

The `Agent` and `BaseAgent` protocols are in `10-core-framework.md`. The `AgentSpec` schema is in `12-config-and-validation.md`. Cross-cutting primitives (connections, caching, retrieval, memory) are in `23`–`26`. This doc ties them together at the agent level.

Three load-bearing properties:

1. **Agents are configuration, not code.** `AgentSpec` + a markdown prompt + a Pydantic output class is everything. The framework constructs and runs the `Agent` from those.
2. **Agent identity is content-hashed.** Any change to prompt pin, model binding, tool allowlist, output schema, or memory config bumps the agent's `version` (a content hash); cached responses, observability identity, and audit references all key on it.
3. **Agents compose, but don't know about composition.** A worker agent doesn't know it's running under a supervisor. The orchestration layer (Tier 3) wires agents into graphs; agents themselves are oblivious. This keeps individual agents independently testable and reusable.

## What an agent IS and IS NOT

| IS | IS NOT |
|---|---|
| A `BaseAgent` instance built from `AgentSpec` | A class users subclass for normal use (only the meta-agent and test fakes subclass directly) |
| Stateless across runs | A long-lived service holding session state |
| The owner of one model binding + one allowlist + one output schema | An orchestrator over multiple agents (that's `SystemSpec.flow`) |
| Versioned via content hash of its config | Versioned via a directory like tools (it's not a shareable artifact in the same way; agents are project-scoped) |
| Composed at runtime by the orchestration compiler into a LangGraph node | A LangGraph node directly authored by the user |

Agent templates can be promoted to `catalog/agent_templates/` for reuse, but agents themselves are project-resident — they reference the project's tools, retrievers, and state.

## Module layout

```
src/foundry/core/
└── agent.py             Agent protocol, BaseAgent, AgentResult, LifecycleHooks

src/foundry/orchestration/
└── compiler.py          AgentSpec → BaseAgent instance with all wirings

projects/<name>/agents/<name>/
├── agent.yaml           AgentSpec
├── prompts/
│   ├── v1.md
│   ├── v2.md
│   └── ...
└── output_schema.py     Pydantic output model
```

The `BaseAgent` instance the runtime uses is constructed by `foundry.orchestration.compiler` — users do not write `class MyAgent(BaseAgent)` for normal agents. They write the `agent.yaml` + the prompt + the output schema; the compiler does the rest.

## The agent directory layout

```
projects/<name>/agents/<agent_name>/
├── agent.yaml             AgentSpec (the manifest)
├── prompts/
│   ├── v1.md              numbered, immutable once committed
│   ├── v2.md
│   └── v3.md              ← agent.yaml.prompt.version pins which is live
├── output_schema.py       Pydantic output model (single file, git-versioned)
└── eval/                  optional — agent-level eval set
    └── default.yaml
```

### `agent.yaml`

The complete `AgentSpec` (full schema in `12-config-and-validation.md`). Realistic example:

```yaml
name: investigator
description: |
  Investigate an incoming exception, gather context from internal
  systems, classify the root cause, and recommend an action.

model_binding:
  provider: anthropic
  model: claude-opus-4-7
  settings:
    max_tokens: 4096
    temperature: 0.2
    cache_control: system_and_tools
  capabilities_required: [tool_use, cache_control, structured_outputs]

prompt:
  version: v3
  path: prompts/v3.md

output:
  schema: output_schema.py::Investigation

tools: [query_snowflake, get_runbook, validate_deltas]

state_visibility:
  read: [messages, current_exception]
  write: [messages, investigation_result]

retry_policy:
  max_attempts: 3
  backoff: exponential
  initial_delay_s: 1.0
  retryable_errors: [ProviderRateLimitError, ProviderTimeoutError]

iteration_limit: 8

semantic_cache:
  enabled: false   # opt-in per agent; default off

retrievers:
  - slot: runbooks
    ref: catalog/hybrid_rrf
    version: v1
    connection_bindings:
      dense_store: prod_pgvector
      sparse_store: prod_elastic
    reranker:
      ref: catalog/cohere_rerank
      version: v1
      connection_bindings:
        cohere: cohere_api
      top_k: 5
    top_k: 30

memory: null   # batch / one-shot agent — no memory

metadata:
  owner: ops-team
  cost_category: investigation

schema_version: 1
```

### `prompts/v<N>.md`

Markdown. Versioned per file (numbered). The agent's prompt pin lives in `agent.yaml`. Rolling back a prompt is a single-line edit to `agent.yaml.prompt.version`.

Recommended prompt structure:

```markdown
# Role
You are an exception investigator for the operations team.

# Persona / behaviour
- Methodical: gather all relevant context before classifying.
- Conservative: when uncertain, escalate to a human rather than auto-resolve.
- Cite sources: every assertion in your output must reference a tool result.

# Available tools
{{TOOL_SUMMARIES}}    ← injected by the framework from ToolSpec descriptions

# Available context
{{MEMORY_PREFIX}}     ← injected by the framework from MemoryConfig (semantic / persona)

# Task
Given the exception in state.current_exception:
1. Query relevant systems via tools to gather context.
2. Classify the root cause from the enum in your output schema.
3. Recommend an action: auto_resolve (under $50k + confidence ≥ 0.85), escalate, or further_investigation.
4. Provide evidence: list every tool call you made and a one-sentence justification.

# Output
Return a single Investigation object matching the declared output schema.

{{MEMORY_SUFFIX}}     ← injected by the framework from MemoryConfig (episodic context)
```

`{{TOOL_SUMMARIES}}` and the memory templates are framework-injected substitutions, not Jinja — the prompt-assembly layer renders them at runtime based on the agent's bound tools and memory config.

### `output_schema.py`

Pydantic model, single file. The output class name is referenced from `agent.yaml.output.schema` (e.g. `output_schema.py::Investigation`).

```python
# output_schema.py
from enum import StrEnum
from pydantic import BaseModel, Field
from datetime import datetime

class RootCause(StrEnum):
    LATE_AMENDMENT = "late_amendment"
    PARTIAL_SETTLEMENT = "partial_settlement"
    SSI_MISMATCH = "ssi_mismatch"
    SYSTEM_OUTAGE = "system_outage"
    UNKNOWN = "unknown"

class RecommendedAction(StrEnum):
    AUTO_RESOLVE = "auto_resolve"
    ESCALATE = "escalate"
    FURTHER_INVESTIGATION = "further_investigation"

class EvidenceItem(BaseModel):
    tool_call_id: str
    summary: str = Field(min_length=10, max_length=500)

class Investigation(BaseModel):
    root_cause: RootCause
    confidence: float = Field(ge=0.0, le=1.0)
    recommended_action: RecommendedAction
    evidence: list[EvidenceItem] = Field(min_length=1)
    cost_if_wrong_usd: float = Field(ge=0.0)
    investigated_at: datetime
```

The output schema is enforced at every agent step. Misshapen output → `OutputValidationError` (a `ToolError`-adjacent class — see § Output validation). The framework supports a single auto-repair attempt before failing.

## `BaseAgent` lifecycle

Already specified at protocol level in `10-core-framework.md`. Recap and expansion at the spec level:

```
session.start
└── compiler.compile_agent(spec) → BaseAgent instance
    ├── resolves model_binding → Provider
    ├── resolves tools allowlist + bindings
    ├── resolves connections (slots → bound connections)
    ├── resolves retrievers (slots → bound retrievers + rerankers)
    ├── resolves memory (layers + injection rules)
    ├── loads output schema class
    ├── resolves prompt version → file content
    └── computes agent_version content hash

agent.run(state, session):
└── _step (called per agent invocation; loops up to iteration_limit times)
    ├── span("foundry.node", agent=name, version=agent_version)
    ├── hooks.before_node
    ├── memory.read(query=...) if memory configured  → MemoryEnvelope
    ├── prompt = assemble_prompt(template, tools, memory_envelope)
    ├── span("foundry.llm")
    ├── (semantic-cache lookup if configured)
    ├── provider.generate(prompt + state.messages, tools, settings)
    │     ├── cost_budget.check(estimated)
    │     ├── (rate limiter wait)
    │     └── (retries on retryable errors per retry_policy)
    ├── cost_budget.record(actual)
    ├── (semantic-cache store)
    ├── parse response:
    │   ├── if tool_use blocks present:
    │   │   ├── span("foundry.tool") for each (parallel via task group)
    │   │   ├── ToolRegistry.dispatch(...)
    │   │   ├── append tool_result blocks to messages
    │   │   ├── increment iteration_count
    │   │   ├── if iteration_count >= iteration_limit: raise IterationLimitError
    │   │   └── loop to next provider.generate call
    │   └── else (terminal response):
    │       ├── extract output text/JSON from content blocks
    │       ├── validate against output_schema (with one auto-repair attempt)
    │       ├── memory.write(message=assistant_response) if memory configured
    │       └── return AgentResult(state_delta=..., output=validated_output)
    ├── hooks.after_node
    └── return AgentResult
```

Every step that emits a `RunEvent` honours the event sequence invariants from `10-core-framework.md` § Streaming events.

## Prompt assembly

The compiler builds the agent's prompt envelope at run time from:

1. **The pinned prompt file** (`prompts/v<N>.md`) — the static body the agent author wrote.
2. **`{{TOOL_SUMMARIES}}` substitution** — formatted from the bound tools' descriptions and input schemas. Standard format:
   ```
   ## query_snowflake
   Run a parameterised read-only SQL query against the bound Snowflake warehouse.
   Inputs: sql (str), parameters (dict), max_rows (int, default 1000)

   ## get_runbook
   Fetch the runbook for a given incident category.
   Inputs: category (str)
   ```
3. **`{{MEMORY_PREFIX}}` / `{{MEMORY_SUFFIX}}` substitutions** — rendered from `MemoryConfig.inject_into_prompt` rules (`26-memory-and-context.md`).
4. **Conversation messages** — `state.messages` (or whatever `MemoryConfig` pulls from).

Final prompt envelope sent to the provider:

```
SystemMessage:
  <static prompt body, with substitutions applied>
  <memory: system_prefix>
  <memory: system_suffix>

ConversationMessages:
  <past messages from working memory or state>
  <current user message>
```

The prompt envelope is observable — `RunEvent.foundry.run.attributes` includes a hash of the assembled system prompt; `capture_inputs: true` projects can attach the full prompt to the run artifact for debug.

## Output validation

The agent's terminal LLM response is parsed into the declared `<OutputModel>`:

1. Extract text content blocks; concatenate.
2. If the schema has a top-level `BaseModel`, attempt direct JSON parse (most providers return structured JSON when `response_format: json_schema` is set).
3. If parse fails, attempt Pydantic validation on the raw text (in case the LLM wrapped JSON in code fences).
4. If validation fails AND `auto_repair: true` (default), make one additional LLM call with the validation error as user input asking the model to fix and resubmit.
5. If still fails: raise `OutputValidationError` with the validator output and the raw text. The orchestration runtime emits `run.failed`.

Auto-repair is bounded: one retry, then fail. This prevents the LLM from looping on a misunderstood schema.

### Repair prompt

```
Your previous response did not validate against the output schema.

Validation errors:
{validation_errors}

Schema:
{schema_json}

Your previous response:
{raw_response}

Please reply with a corrected response that validates.
```

### Schema-aware tool exclusion during repair

During auto-repair, the framework strips the tool list from the prompt — the agent has finished tool use; this is just a format fix. Prevents a repair attempt from kicking off a new tool-use round.

## Iteration limit

`AgentSpec.iteration_limit: int = 20` caps the number of LLM-call rounds per `agent.run()` invocation. Each round = one LLM call (+ any parallel tool calls in that round's response). Exceeding the limit raises `IterationLimitError` (under `OrchestrationError`).

Tuning guidance:
- Single-turn tools-free agent: 1 (set explicitly to fail loud if anything tries multi-turn).
- Standard agent with 1–3 tool calls expected: 5–8.
- Investigation agent that may need many tool calls: 15–25.

The limit is per-invocation, NOT per-run. A supervisor calling a worker 3 times invokes the worker 3 times; each invocation has its own iteration budget.

## Multi-tool-call (parallel) handling

Anthropic and OpenAI both support multiple tool-use blocks in a single response. The framework dispatches them via `anyio.create_task_group`:

```
LLM response:
  - tool_use(id=t1, name=query_snowflake, input={...})
  - tool_use(id=t2, name=get_runbook, input={...})

Framework:
  async with create_task_group() as tg:
      tg.start_soon(dispatch_tool, t1, ctx)
      tg.start_soon(dispatch_tool, t2, ctx)

  # both complete (or one fails and cancels siblings cleanly)

  Next user message includes:
    - tool_result(id=t1, content=[...])
    - tool_result(id=t2, content=[...])
```

Failure semantics:
- One tool succeeds, one fails → `tool_result(id=tFailed, is_error=true, content=[...])`. LLM sees both; can decide what to do.
- If `failure_mode: cancel_siblings` is set on the agent (default), a `RunCancelled` from one tool cancels in-flight siblings cleanly.
- If `failure_mode: collect_all`, the framework awaits all parallel tools regardless of failures, returning per-tool results to the LLM.

## Agent archetypes

These are *patterns* (not framework primitives). The orchestration layer (`30-orchestration-patterns.md`) wires them together. From an agent's own perspective, all archetypes look the same — `BaseAgent.run()`. The differences are in how the agent's tool set, prompt, and output are shaped.

### Worker agent

Single-purpose. Reads narrow input from state, calls a few tools, returns structured output. Doesn't know about other agents.

```yaml
name: break_detector
tools: [query_trade_db, validate_amounts]
output:
  schema: output_schema.py::DetectedBreak
state_visibility:
  read: [current_event]
  write: [detected_breaks]
iteration_limit: 5
```

### Supervisor agent

Routes between worker agents. Tools include "handoff" tools — `transfer_to_<worker_name>` — generated by the orchestration compiler from the supervisor's `workers` list in `SystemSpec.flow`. The supervisor's prompt explains when to hand off to whom.

```yaml
name: orchestrator
tools: []   # actual tools come from the flow's handoff config; see 30-orchestration-patterns.md
output:
  schema: output_schema.py::FinalDecision
state_visibility:
  read: [messages, intermediate_results]
  write: [final_decision]
iteration_limit: 30   # higher because supervisor coordinates multiple worker turns
```

### Router agent

Stateless classifier. Single LLM call, no tools, picks one of N branches via output enum. Used in `flow.type: graph` conditional edges where the routing decision needs LLM judgement.

```yaml
name: severity_router
tools: []
output:
  schema: output_schema.py::SeverityDecision   # enum: low / medium / high
iteration_limit: 1
```

### Synthesiser / writer agent

Reads a lot of context (often from retrieval), produces a long-form output (report, summary, draft). Heavy on prompt design, light on tools. Often configured with semantic cache because identical contexts → identical reports.

```yaml
name: report_writer
tools: []
retrievers:
  - slot: source_docs
    ref: catalog/hybrid_rrf
    version: v1
    connection_bindings: {dense_store: ..., sparse_store: ...}
output:
  schema: output_schema.py::Report
semantic_cache:
  enabled: true
  embedder_binding: {provider: voyage, model: voyage-3}
  similarity_threshold: 0.97
  ttl_s: 7200
```

### Conversational agent

User-facing. Multi-turn. Memory layers configured. Persona in prompt. Often has a wide tool surface to handle varied requests.

```yaml
name: support_assistant
tools: [search_kb, look_up_account, escalate_to_human]
memory:
  layers:
    - kind: working
      name: short_term
      source_field: messages
      window: {max_messages: 12}
    - kind: episodic
      name: past_conversations
      retriever_slot: history
      top_k: 5
    - kind: semantic
      name: user_preferences
      state_field: synthesised_preferences
      consolidate_every_n_turns: 8
      consolidator_prompt: prompts/consolidate_prefs.md
output:
  schema: output_schema.py::AssistantReply
iteration_limit: 8
```

## Agent identity and versioning

`agent_version` is a content hash over the agent's full configuration:

```
agent_version = sha256(
    canonical_json({
        "model_binding": ...,            # provider, model, settings, capabilities_required
        "prompt": file_content("prompts/v<pinned>.md"),
        "output_schema": file_content("output_schema.py"),
        "tools": sorted(tools_allowlist),
        "tool_bindings": {                # version-pinned tool bindings the agent uses
            name: f"{ref}@{version}"
            for name, binding in resolved_tools.items()
        },
        "state_visibility": {"read": sorted(read), "write": sorted(write)},
        "iteration_limit": ...,
        "semantic_cache": ...,            # if configured
        "retrievers": ...,                # if configured
        "memory": ...,                    # if configured
        "schema_version": 1,
    })
)[:16]                                    # first 16 hex chars; sufficient uniqueness for foundry scope
```

Used in:
- `RunEvent.AgentStarted.agent_version`
- `SemanticCacheKey.agent_version` — invalidates cache on any config change
- Audit log entries
- `foundry agent inspect <project>/<agent>` output

Any edit to the agent's config or its pinned files changes `agent_version`. Roll-forward and rollback both produce a new content hash because the pin changes.

## State visibility (recap; full spec in `22-state-management.md`)

Every agent declares `read` and `write` lists against `StateSpec.schema` field names. The compiler:

1. Validates that each named field exists in `StateSpec.schema`.
2. Generates a LangGraph subgraph for the agent with:
   - **Input schema**: `TypedDict` containing only the `read` fields.
   - **Output schema**: `TypedDict` containing only the `write` fields.
3. At run time, the orchestration layer projects the full state down to the agent's input schema; outputs are merged into full state per the field reducers.

Attempting to read a forbidden field at runtime is impossible — the field literally isn't in the agent's view. Attempting to declare a `read`/`write` field that doesn't exist in `StateSpec` → compile-time `StateVisibilityError`.

## Composition with cross-cutting primitives

| Primitive | How agent consumes |
|---|---|
| **Provider** | `model_binding` resolved at compile to a `ProviderAdapter` instance held by the agent. |
| **Tools** | `tools` allowlist + `SystemSpec.tools` bindings → `ToolRegistry` registers; agent dispatches via dispatcher. |
| **Connections** | Indirect — tools and retrievers declare slots; project binds; agent never touches connections directly. |
| **Embedder** | Indirect — semantic cache + episodic memory configure embedder bindings; agent never embeds directly. |
| **Retriever / Reranker** | `retrievers` field on AgentSpec; framework wires `ctx.retrievers.get(slot)`. |
| **Semantic cache** | `semantic_cache` field; framework wraps `provider.generate` with cache lookup/store. |
| **Memory** | `memory` field; framework runs `memory.read` before each LLM call, `memory.write` after, and `memory.consolidate` on configured triggers. |
| **Cost budget** | Indirect — provider adapter checks/records against `Session.cost_budget`. |

The agent declares what it wants; the framework wires the orchestration. The agent's own code (the prompt + the output schema) stays domain-focused.

## `build_agent` scaffold (meta-agent path)

When the meta-agent decides to create a new agent, it calls `build_agent`. The scaffold creates:

```
projects/<scoped_project>/agents/<name>/
├── agent.yaml          ← AgentSpec stub with name, description placeholders
├── prompts/
│   └── v1.md           ← prompt skeleton
└── output_schema.py    ← empty Pydantic class stub
```

The meta-agent fills in:

1. **`output_schema.py`** — typed output model based on the use case.
2. **`agent.yaml`** — model binding, tool allowlist (from catalog discovery), state visibility, retry policy, iteration limit.
3. **`prompts/v1.md`** — system prompt drafted to the use case + tool summaries + output instructions.

Then runs the project's eval set; iterates the prompt (creating `v2.md`, `v3.md`, ...) until threshold or max iterations.

### Scaffold guardrails (meta-agent only)

- Output schema must have at least one field. Empty outputs rejected.
- `iteration_limit` defaulted to 8; meta-agent prompt warns against >25 without justification.
- `semantic_cache.enabled: false` by default; meta-agent prompt explains the correctness risk and requires explicit operator opt-in.
- `provider_overrides` not populated (per locked decision in `00-vision-and-scope.md`).
- Memory not configured by default — only added if the use case is conversational and the user explicitly requests it.

## Agent-level eval

In addition to the project-level end-to-end eval, agents can have their own eval set under `projects/<name>/agents/<agent>/eval/`. Run via `foundry eval agent <project> <agent_name>`.

Cases test the agent in isolation:
- Input is a state slice matching the agent's `read` visibility.
- Expected output is a structured value matching the output schema.
- Scorers are agent-appropriate: exact match for classifiers, LLM-judge for generative outputs, rubric for structured-but-flexible outputs.

This complements (doesn't replace) project-level eval. Agent-level evals catch regressions per agent without re-running the whole pipeline.

## Catalog agent templates

`catalog/agent_templates/<name>/v<N>/` is reserved for reusable agent archetypes (router, summariser, classifier). Empty in v1; templates added when patterns repeat across projects.

Template shape mirrors the project agent directory but is parameterised:

```
catalog/agent_templates/router/v1/
├── template.yaml        AgentSpec template with placeholders
├── prompts/
│   └── v1.md.template   prompt with substitution markers
└── output_schema.py.template
```

The meta-agent's `build_agent --from-template router/v1` copies the template into the project, runs substitutions, and lets the meta-agent customise from there.

## Failure modes

| Cause | Surfaced as |
|---|---|
| Pinned prompt file missing | `ConfigLoadError` at compile |
| `output.schema` import fails | `ConfigError` at compile |
| Tool in `tools` allowlist not in `SystemSpec.tools` | `CompileError` |
| `state_visibility` references unknown field | `StateVisibilityError` at compile |
| `iteration_limit` reached during run | `IterationLimitError` (under `OrchestrationError`) |
| Output validation fails after auto-repair | `OutputValidationError` (under `OrchestrationError`) |
| Provider call fails (after retries) | `ProviderError` subclass propagates |
| `CostBudgetExceeded` raised pre-call | run terminates cleanly with `RunFailed` |
| Memory layer fails (default mode) | empty contribution + warning event; run continues |
| Memory layer fails (`fail_strict: true`) | `MemoryLayerError` |
| Cache backend down | fail-open + warning; run continues |

Every failure emits a structured `RunEvent` for the audit trail.

## Invariants

1. **Agent has exactly one model binding.** Cascading / fallback to a different model is an orchestration concern (router agent + flow), not an agent concern.
2. **Agent has exactly one output schema.** Different output shapes mean different agents.
3. **Agent's tool allowlist is enforced at dispatch.** Configured at compile, checked at every dispatch.
4. **`agent_version` is content-hashed over all config + pinned files.** Any config change produces a new hash.
5. **The pinned prompt file must end with `<version>.md`** (the same content-consistency check from `12-config-and-validation.md`).
6. **Auto-repair is exactly one retry.** No multi-turn repair loops.
7. **`iteration_limit` is per-invocation, not per-run.** A supervisor calling a worker N times invokes the worker N times, each with its own budget.
8. **Agents do not directly mutate state.** They return `AgentResult` with `state_delta`; the orchestration runtime applies deltas.
9. **`BaseAgent.run` always emits `agent.started` and a matching `agent.completed`** (or a `run.failed` if it dies).
10. **Memory writes from agent output are explicit.** The framework writes the assistant message to working memory automatically; semantic-layer writes require the agent to explicitly call `memory.write` (typically via a `remember` tool or via consolidation).

## Test expectations

### Unit

1. **AgentSpec round-trip**: load → dump → re-load equality.
2. **`agent_version` determinism**: same config + pinned file content → same hash; any change → different hash.
3. **State visibility validator**: `read: [unknown_field]` → `StateVisibilityError` at compile.
4. **Tool allowlist validator**: tool name not in `SystemSpec.tools` → `CompileError`.
5. **Output validation auto-repair**: malformed first response → repair attempt with validation errors → valid second response → success.
6. **Output validation failure after repair**: malformed twice → `OutputValidationError`.
7. **Iteration limit**: agent that always emits a tool_use block + `iteration_limit: 3` → `IterationLimitError` after 3 LLM calls.
8. **Parallel tool dispatch**: response with 3 tool_use blocks → 3 dispatches via `create_task_group`; one failure with `failure_mode: cancel_siblings` → other two cancelled cleanly.

### Contract

1. **`{{TOOL_SUMMARIES}}` substitution**: agent with 3 tools → assembled prompt contains all 3 tool descriptions in expected format.
2. **Memory injection**: agent with all 3 memory layers configured → assembled prompt contains envelope contributions per `inject_into_prompt` rules.
3. **No `RunContext` leak**: agent code doesn't store `ctx`; lint check.

### Integration (Phase 2 exit gate)

1. **End-to-end agent run**: a hello-world agent loads, compiles, runs against a fake provider, returns validated output, emits all expected `RunEvent`s.
2. **Agent identity invalidation**: bump `prompts/v3` → `v4` (pin change), `agent_version` changes; semantic-cache lookup miss; clean re-run.
3. **Worker reusability**: same agent config used in two different `SystemSpec.flow` topologies; both runs produce identical outputs given identical state.

## Function nodes (the non-LLM alternative)

Not every node in a flow needs an LLM. Input normalisation, output formatting, deterministic business-rule gates, pure data transformations — these benefit from being explicit nodes (state-visibility-enforced, observed, retryable, checkpointed) but don't need a model call.

`FunctionNode` is the deterministic-Python alternative to `Agent` at the same flow position. Both implement the `Node` protocol; the orchestration compiler accepts either interchangeably. Spec details: `10-core-framework.md` § FunctionNode protocol; schema: `12-config-and-validation.md` § FunctionNodeSpec.

### Directory shape

```
projects/<name>/functions/<node_name>/
├── function.yaml      FunctionNodeSpec (function ref + state visibility + retry/timeout)
├── function.py        async def <name>(state_view, ctx) -> dict[str, Any]
└── README.md          what it does, when to use
```

### Realistic example

```yaml
# function.yaml
name: normalize_input
description: |
  Normalise incoming break records into the canonical shape the
  downstream agents expect. Trims whitespace; coerces date formats;
  computes derived fields (severity_band).

function: function.py::normalise

state_visibility:
  read: [raw_input]
  write: [normalized_input, severity_band]

retry_policy:
  max_attempts: 1     # function should be deterministic; retries usually pointless

timeout_s: 2.0
```

```python
# function.py
from foundry import RunContext

async def normalise(state_view: dict, ctx: RunContext) -> dict:
    raw = state_view["raw_input"]
    return {
        "normalized_input": {
            "trade_id": raw["trade_id"].strip().upper(),
            "amount_usd": float(raw["amount"]),
            "timestamp": parse_timestamp(raw["timestamp"]),
        },
        "severity_band": classify_severity(raw["amount"]),
    }
```

Used in flow:

```yaml
# system.yaml
agents: [classifier, investigator, resolver]
functions: [normalize_input, format_response]

flow:
  type: sequential
  steps: [normalize_input, classifier, investigator, resolver, format_response]
```

The compiler resolves each step name to either an agent or a function node. Names cannot collide across the two namespaces (compile-time check).

### What function nodes DO have

- State visibility (same enforcement as agents).
- Retry policy + timeout (same plumbing).
- `RunContext` access (session, connections — for function nodes that need to call external systems via the same pooled connections).
- Lifecycle hooks (same instrumentation surface).
- Their own `RunEvent`s (`function_node.started`, `function_node.completed`).
- Content-hashed `node_version` (over function source + config).

### What function nodes DO NOT have

- Model binding, prompt, output schema (no LLM).
- Tools allowlist (function nodes can't call tools — they're meant to be self-contained Python).
- Memory or semantic_cache (no LLM, nothing to cache against the input meaningfully).
- Iteration limit (functions run exactly once per invocation).
- `OutputValidationError` auto-repair (no LLM to repair the output).

### When to choose FunctionNode over Agent

- The decision is deterministic from state. No LLM judgement adds value.
- Latency or cost matters and the work is mechanically expressible.
- The transformation is shape-changing (e.g., transforming a list of agent outputs into a structured report).
- A business-rule gate ("if state.x < 100, skip; else continue") that doesn't need natural-language reasoning.

### When NOT to choose FunctionNode

- The decision needs LLM judgement (classification with grey areas, summarisation, free-text generation). Use Agent.
- You need tool calls. Use Agent (with tools allowlist).
- The "function" would be inventing complex logic the meta-agent could handle in a prompt. Prefer Agent for rapid iteration via prompt edits.

The asymmetry: agents are configuration + prompt (cheap to iterate); function nodes are configuration + Python (handmade, requires engineering review). Use FunctionNode when determinism is the goal; Agent when flexibility is the goal.

### `build_function_node` (meta-agent path)

The meta-agent's `build_function_node` tool scaffolds:

```
projects/<scoped_project>/functions/<name>/
├── function.yaml      stub
├── function.py        empty async function with correct signature
└── README.md          templated
```

The meta-agent fills in:
1. State-visibility lists (read/write).
2. The function body — usually small, deterministic transformations.

Then runs the project eval to confirm the function integrates correctly. No standalone eval for function nodes (they're tested via the project eval; their determinism makes regressions instantly visible).

Same guardrails apply: meta-agent doesn't write `subprocess`, `eval`, `exec` (lint catches), doesn't bypass connection slots if external system access is needed.

## Open questions

1. **Per-agent `LifecycleHooks` config**. Agents currently inherit project-level hooks via `Session`. Should an agent be able to declare its own hooks (e.g., a custom `before_node` that mutates state)? Lean: no in v1 — keep hooks instrumentation-only. Revisit if needed.
2. **Output-schema repair loop limit**. Currently exactly one retry. Should this be configurable (1–3)? Lean: no — multi-attempt repair masks bad schemas. One attempt + clear error is correct.
3. **Agent-level cost budget overrides.** Currently cost budget is project-wide. Should agents be able to declare their own (e.g., supervisor cap of $0.10 per invocation)? Lean: yes, additive — `AgentSpec.max_cost_per_invocation_usd: Decimal | None`. Useful for parallel-agent scenarios where one runaway agent could blow the project budget.
4. **Streaming output mid-tool-use**. Some patterns (chat) want the agent's text emitted as `LLMDelta` events while tool calls are pending. Currently tool dispatch awaits the full response. Lean: keep current behaviour for v1; revisit if real conversational use cases need it.
5. **Subgraph-as-agent**. A "compound agent" that internally runs a small sub-graph. Possible via the supervisor pattern in orchestration; should there be a primitive that wraps a sub-graph as a single `BaseAgent` for re-use? Lean: no in v1; revisit if patterns repeat.
