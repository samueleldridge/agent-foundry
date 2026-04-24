# 02 — Framework Evaluation

## Purpose

The single biggest architectural decision in agent-foundry is the choice of agent execution runtime. This doc documents the evaluation, the decision, the accepted tradeoffs, and the escape hatches we keep open. It is *the* artefact to cite when someone asks "why LangGraph, not X?"

## Decision (TL;DR)

**LangGraph is the single core execution runtime for agent-foundry v1.**

Non-hybrid. A hybrid (PydanticAI for single-agent + LangGraph for multi-agent) was considered and rejected because maintaining two agent mental models in one codebase violates the *small orthogonal abstractions* principle (`00` § 10) and produces less leverage, not more. We retain the ability to add a PydanticAI adapter later *if and only if* single-agent ergonomics on LangGraph prove painful in practice — see § Escape hatches.

## Requirements (re-stated)

The runtime must support (or be cheap to extend to):

1. Async execution throughout.
2. Pydantic v2 runtime validation.
3. Provider-agnostic LLM calls (Anthropic, OpenAI, Bedrock, Azure, others).
4. Configurable per-node / per-sub-agent state visibility, expressible in YAML.
5. Checkpointing / persistence of run state.
6. Human-in-the-loop interrupts.
7. Observability (tracing of tool calls, model calls, state transitions).
8. Multi-agent orchestration patterns (supervisor, handoff, parallel, sequential).
9. Config-driven agent construction.
10. Reasonable to wrap — a declarative foundry layer above it must not fight the runtime's abstractions.

Requirements 5, 6, 7, 8 together are the hardest to build from scratch — they are weeks to months of work. Any framework that gives them to us out of the box saves more engineering than we spend on whatever its quirks cost us.

## Candidates evaluated

| # | Framework | Status |
|---|---|---|
| 1 | **LangGraph** (LangChain Inc.) | ✅ Chosen |
| 2 | PydanticAI (Pydantic team) | Rejected as core; retained as potential adapter |
| 3 | OpenAI Agents SDK (Swarm successor) | Rejected — provider-agnosticism too shallow |
| 4 | CrewAI | Rejected — opinionated role/goal abstractions fight wrapping |
| 5 | AutoGen / AG2 | Rejected — fragmentation risk + heavy actor model |
| 6 | LlamaIndex Workflows | Rejected — event-driven API hard to generate from YAML |
| 7 | Anthropic Claude Agent SDK | Rejected — Anthropic-first by design, fails requirement 3 |
| 8 | Pure custom on provider SDKs | Rejected — requirements 5/6/8 cost too much to build |

## Decision matrix

| Framework | 1 Async | 2 Pyd v2 | 3 Multi-provider | 4 Scoped state (config) | 5 Checkpoint | 6 HITL | 7 Observe | 8 Multi-agent | 9 Config-driven | 10 Wrappable |
|---|---|---|---|---|---|---|---|---|---|---|
| LangGraph | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⚠️ |
| PydanticAI | ✅ | ✅ | ✅ | ⚠️ | ⚠️ | ⚠️ | ✅ | ⚠️ | ✅ | ✅ |
| OpenAI Agents SDK | ✅ | ✅ | ⚠️ | ⚠️ | ❌ | ⚠️ | ✅ | ✅ | ⚠️ | ✅ |
| CrewAI | ⚠️ | ✅ | ✅ | ⚠️ | ❌ | ⚠️ | ⚠️ | ✅ | ✅ | ⚠️ |
| AutoGen / AG2 | ✅ | ✅ | ✅ | ⚠️ | ⚠️ | ✅ | ⚠️ | ✅ | ⚠️ | ⚠️ |
| LlamaIndex Workflows | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⚠️ |
| Claude Agent SDK | ✅ | ⚠️ | ❌ | ⚠️ | ⚠️ | ✅ | ⚠️ | ⚠️ | ✅ | ⚠️ |
| Pure custom | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ⚠️ | ❌ | ✅ | ✅ |

Legend: ✅ supported natively · ⚠️ supported with caveats or DIY · ❌ not supported.

No framework scored ✅ on all 10. The question is which gaps we can afford.

## Per-framework summaries

### LangGraph

Graph-based agent runtime from LangChain Inc. Models execution as a directed graph of nodes over a typed `State` object. Nodes are async functions. Edges are either unconditional or conditional (routing function returns the next node). Checkpointers persist state after each node (in-memory, SQLite, Postgres, Redis). `interrupt()` pauses a run for human input; `Command(resume=...)` resumes.

**State model.** A single typed `State` (TypedDict or Pydantic model) with reducer semantics per field — `Annotated[list[Message], add_messages]` concatenates, a bare field is last-write-wins. Scoping to sub-agents is done via subgraphs with their own input/output schemas, or by nodes that explicitly project state. **No YAML-level scoping in the box** — this is the single biggest gap, and it is exactly the gap the foundry's config layer closes.

**Provider-agnosticism.** Via LangChain's `init_chat_model`. Covers Anthropic, OpenAI, Bedrock, Vertex, Azure, Mistral, Groq, Ollama natively. Provider-specific features (Anthropic cache control, OpenAI structured outputs, reasoning tokens) reachable via `model_kwargs` / `extra_body` — usable but rough. The foundry's provider abstraction layer (`11-provider-abstraction.md`) will wrap these cleanly.

**Top 2 pros (for wrapping):**
1. **Checkpointing, interrupts, and multi-agent primitives are the most production-grade in this list.** These are months of engineering we inherit for free. No other framework delivers all three at this quality level.
2. **Graph abstraction fits agents.** An agent run *is* a sequence of LLM calls and tool calls with conditional routing. LangGraph's core primitive matches that directly, rather than anthropomorphizing (roles, crews) or hiding it (run-until-done loops).

**Top 2 cons (for wrapping):**
1. **No native YAML graph spec.** Graphs are built imperatively in Python. Our config → graph compiler must generate Python constructor calls or equivalent wiring. This is non-trivial but it is work we were going to do anyway — the declarative layer is the whole point of the foundry.
2. **LangChain surface area leaks.** Messages, runnables, callbacks come in through the door whether you want them or not. Managing this requires drawing strict boundaries in the foundry's core module and never re-exporting `langchain_core` types from public APIs.

### PydanticAI

Pydantic's own agent framework. Type-first, Pydantic-v2-native, async-first. `Agent` class with decorated tool functions and typed outputs. No global graph state — each agent has a typed `Deps` object injected via `RunContext`.

**Why rejected as core.** You build checkpointing (weeks), HITL interrupt semantics (weeks), supervisor/parallel/router patterns (weeks). For a personal tool where time-to-first-working-system is the success metric, that's too much plumbing. Its strengths (Pydantic-native, cleanest YAML mapping, smallest surface area) are real — which is why we keep it as a potential adapter. See § Escape hatches.

### OpenAI Agents SDK

Official OpenAI agents SDK (`openai-agents`), successor to Swarm. Clean primitives: `Agent`, `Runner`, `handoff`, `guardrails`, tools. Excellent built-in tracing dashboard.

**Why rejected as core.** Provider-agnosticism is "OpenAI-first with a shim" — non-OpenAI models work via the Chat Completions adapter, but Responses API features (built-in tools, reasoning summaries) are OpenAI-only. For client work where Anthropic is primary, this is a real tax. Also no built-in checkpointer.

### CrewAI, AutoGen/AG2, LlamaIndex Workflows, Claude Agent SDK

Summarised briefly — see `memory/reference_framework_research.md` for the full analysis.

- **CrewAI** — YAML-first and easy to prototype, but role/goal/backstory abstractions fight the wrapping; no production-grade checkpointer.
- **AutoGen / AG2** — strong multi-agent; fragmentation risk between the Microsoft and community forks; heavier actor abstraction than we want under a declarative layer.
- **LlamaIndex Workflows** — good event-driven model; identity as a RAG framework leaks into abstractions; event-handler style harder to generate from YAML than agent-as-tool models.
- **Claude Agent SDK** — great if you only ever use Anthropic; disqualifying on requirement 3 for FDE work with client-mandated providers.

### Pure custom

Write our own loop on the `anthropic` + `openai` SDKs. Perfect fit, zero version-churn pain, but requirements 5/6/8 are 2–4 months of plumbing. Unjustifiable for a personal tool whose value is *speed*.

## Justification for choosing LangGraph

Stated as a series of falsifiable claims. If any turns out false in practice, revisit.

### Claim 1: Requirements 5, 6, 7, 8 are the expensive ones.

Anyone can build a tool loop that calls an LLM and dispatches tool calls — requirements 1–3, 9, 10 are a week of work on any reasonable provider SDK. Requirements 4, 5, 6, 7, 8 are the ones that separate a toy from a tool that can actually be used on real client systems. LangGraph gives us 5/6/7/8 at production quality today. Building them ourselves or on top of a framework that doesn't provide them would consume the time we are trying to save.

### Claim 2: Requirement 4 is what the foundry solves anyway.

The per-node state visibility gap (⚠️ on LangGraph) is not a gap we fill by picking a different framework — no framework in the list has native YAML state-scoping. It's a gap we fill by building the declarative config layer on top, which we are building either way. LangGraph's subgraph + typed input/output schema machinery is actually *more* powerful under the hood than any competitor for this purpose — we just have to expose it via config.

### Claim 3: Requirement 10 (wrappability) is a caveat, not a blocker.

LangGraph's LangChain surface-area leak is real. The mitigation is a strict module boundary: only `foundry/runtime/langgraph_adapter.py` imports LangGraph directly; every other module in the foundry uses foundry-native types. This is standard hexagonal-architecture discipline and it is enforced by lint rule (`ruff` import boundary check — see `10-core-framework.md`).

### Claim 4: Hybrid increases surface area, not leverage.

Hybrid (PydanticAI leaf + LangGraph orchestrator) is defensible on paper. In practice it means:
- Two provider abstractions to keep in sync (LangChain's `init_chat_model` + PydanticAI's model classes).
- Two tool-schema representations to map between.
- Debugging sessions that cross framework boundaries.
- Two upgrade treadmills.

The theoretical Pydantic-native single-agent wins are eclipsed by the operational cost of maintaining two stacks in a personal tool. Pick one.

### Claim 5: The hard parts don't come from the framework; they come from our own layer.

Once the framework is chosen, the interesting engineering of the foundry is in: the config schema, the eval harness, the meta-agent's prompt and tools, the versioning model, the rollback semantics. These are framework-agnostic. Changing framework later (if we're wrong) affects `langgraph_adapter.py` and some orchestration details, not the bulk of the codebase.

## Accepted tradeoffs (what we're buying at a cost)

We are explicit about the costs so we are not surprised later.

### A. API churn

LangGraph minor releases have historically shifted checkpointer interfaces, interrupt semantics (old `NodeInterrupt` → new `interrupt()`), and prebuilt agents. We accept this cost and mitigate with:
- **Pin versions in `pyproject.toml`.** No `>=` on LangGraph or `langgraph-*` packages. Exact `==` pins, upgraded deliberately.
- **Adapter module isolates the pain.** `foundry/runtime/langgraph_adapter.py` is the one place API changes touch.
- **Contract tests** that exercise checkpointer, interrupts, streaming, and model-provider bindings — run before every dependency upgrade.

### B. State-reducer subtlety

`Annotated[list, add_messages]` et al. are powerful but easy to get wrong; silent state overwrites are a known pitfall. Mitigation: every state schema in the foundry is a Pydantic model with explicit reducer metadata, validated at compile time (see `22-state-management.md`). Direct `TypedDict` state definitions are prohibited.

### C. Message-centric defaults

LangGraph's `create_react_agent` and many examples assume a `messages: list[BaseMessage]` state field. Our state schema is *not* message-centric by default — it's task-centric and configured per system. We will write our own node implementations that use messages as one field among several. Tutorial copy-paste will be misleading; our examples must be first-class.

### D. LangChain surface area

`langchain_core.messages`, `langchain_core.tools`, `langchain_core.runnables` will be imported somewhere. Mitigation: the foundry's public API does not re-export any `langchain_*` or `langgraph` type. If a foundry user writes `from foundry import X`, X is a foundry type. Internal conversions happen in the adapter.

### E. Postgres checkpointer migrations

If we deploy with Postgres checkpointing, schema migrations across LangGraph versions have bitten users. Mitigation: use SQLite checkpointer for local development and a version-locked Postgres deployment for any production-ish use; always back up checkpoint DB before upgrading LangGraph.

## Escape hatches

We are not married forever. These are the revisit conditions and the migration paths.

### Escape hatch 1: PydanticAI adapter for single-agent workloads

**Condition to trigger:** building a single-agent system on LangGraph feels heavy (e.g., we're writing 30 lines of graph wiring for a 5-line agent) across ≥3 real systems.

**Migration path:** introduce `foundry/runtime/pydantic_ai_adapter.py` alongside `langgraph_adapter.py`. Agent config gains a `runtime: langgraph | pydantic_ai` field. Default remains `langgraph`. The foundry's `Agent` protocol already abstracts runtime — this is an additive change, not a rewrite.

**Risk:** we end up with two stacks after all. Mitigation: adopt only if the leverage gain is measurable (lines of code reduced, bugs reduced, cycle-time improved). Not for aesthetic reasons.

### Escape hatch 2: Switch framework entirely

**Condition to trigger:** LangGraph breaks a hard requirement — multi-provider stops working, async goes sideways, or the project stagnates.

**Migration path:** because `langgraph_adapter.py` is the only place LangGraph is imported, switching means rewriting that file and re-pointing the foundry's `Agent` protocol at a new adapter. Config files, eval sets, prompts, tool definitions, meta-agent — all survive. Upper bound: ~2 weeks of focused work.

### Escape hatch 3: Downgrade to custom

**Condition to trigger:** LangGraph's primitives prove wrong-shaped for our domain AND no other framework fits.

**Migration path:** implement minimal checkpointer + interrupt primitives ourselves, targeting the same `Agent` protocol. This is a several-month investment; we don't take it lightly.

## Implications for the foundry layer

### Provider abstraction (`11`)

LangGraph delegates LLM calls to LangChain's `init_chat_model`. Our `foundry.providers` module wraps `init_chat_model` behind a foundry-native `Provider` interface that:
- Exposes capabilities (`supports_cache_control`, `supports_thinking`, `supports_structured_outputs`) as typed properties.
- Takes a `ModelBinding` Pydantic model (provider + model + settings) and returns a LangChain chat model with the right kwargs applied.
- Is the only module in the foundry that knows about provider-specific kwargs.

### Config → graph compiler (`31`, `10`)

The orchestration layer's compiler is the most algorithmically interesting piece of v1. It takes a validated `SystemSpec` Pydantic model and produces a compiled `StateGraph`. The compiler:
- Maps each agent spec to a node (or a subgraph, if the agent has its own internal graph).
- Enforces state visibility by generating subgraphs with input/output schemas matching the visibility config.
- Wires edges from the declarative flow spec (sequential, parallel, conditional with a router function, etc.).
- Produces a `CompiledSystem` that wraps the `StateGraph` plus metadata (run id source, checkpoint location, observability hooks).

### State schema (`22`)

State is Pydantic, always. The compiler generates the LangGraph-native state TypedDict from the Pydantic model via introspection, with reducers read from field metadata (`Annotated[..., Reducer.APPEND]` etc.). This keeps all schema authoring in Pydantic and hides LangGraph's reducer syntax from the user.

### Checkpointing (`71`, `81`)

v1 default: SQLite checkpointer at `~/.foundry/checkpoints/<run_id>.db`. Configurable per-system. Postgres supported with explicit config. The checkpointer is surfaced to the user only via the `run_id` — every CLI command that runs an agent prints the run id and the checkpoint location.

### HITL (`32`)

LangGraph's `interrupt()` maps to a foundry-native `ApprovalRequired` exception raised inside a node. The foundry's CLI and API both surface this as a pending run; the user resumes via `foundry resume <run_id> --approve` (or `--reject --reason "..."`). Approvals are logged in the run artifact.

### Observability (`80`)

LangSmith integration is optional — off by default, on with `FOUNDRY_TRACING=langsmith` and an API key. Default tracing is OTel, exported to any OTel-compatible backend. Structured run artifact is written regardless.

## Test expectations for the decision

Before merging Tier 1 implementation, these tests MUST pass. Failure means the decision is wrong and we re-open the evaluation.

1. A trivial agent (one model call, one tool) runs end-to-end on LangGraph under the foundry's `Agent` protocol in under 10 lines of foundry user code.
2. The same agent runs against Anthropic `claude-opus-4-7` and OpenAI `gpt-5` (or equivalent) with no changes to the agent config body — only the `model_binding` field differs.
3. An agent run is interrupted mid-way, the process is killed, the checkpoint survives, a new process resumes the run to completion.
4. A supervisor/worker system with scoped state (worker cannot read supervisor's scratchpad) is expressible in YAML and the compiler raises a `StateVisibilityError` if a worker tries to read a forbidden field.
5. No `langchain_*` or `langgraph` types appear in any `foundry/**/*.py` module outside `foundry/runtime/langgraph_adapter.py` and `foundry/runtime/_langgraph_types.py` (the import-boundary lint).

## Open questions

- **LangGraph version pin.** Which exact version is the v1 target? Decide at start of Phase 1 implementation; document in `10-core-framework.md`.
- **LangSmith default.** On by default for development (it's convenient) or off by default (no external service unless opted-in)? Recommend off by default, opt-in via env.
- **Checkpointer backend default for tests.** In-memory for unit tests, SQLite for integration tests. Any objection?

## References

- `memory/reference_framework_research.md` — full research summary preserved as starting input for this doc.
- LangGraph: `https://langchain-ai.github.io/langgraph/`
- PydanticAI: `https://ai.pydantic.dev/`
- OpenAI Agents SDK: `https://openai.github.io/openai-agents-python/`
