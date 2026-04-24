# 00 — Vision and Scope

## What agent-foundry is

`agent-foundry` is a Python developer kit for constructing, evaluating, versioning, and orchestrating multi-agent LLM systems from declarative configs. It is a personal tool owned by a Lead AI engineer whose primary value add is integration of AI into existing enterprise systems — FDE-style work. The foundry automates the scaffolding, testing, and iteration work that would otherwise eat weeks per deployment, so the engineer can spend that time on integration.

## What it is not

- **Not a production agent runtime.** The runtime that executes agents in production is the set of configured artifacts this tool produces, deployed into a target system. The foundry itself is a dev-time tool.
- **Not a hosted platform.** No service to stand up, no multi-tenant concerns, no user management. It is a library + CLI that runs on a laptop or a CI runner.
- **Not a new agent framework.** It sits *on top of* a chosen agent runtime (LangGraph, see `02-framework-evaluation.md`). The foundry's novelty is in the declarative config layer, the eval harness, the versioning model, and the meta-agent — not in inventing another agent-execution abstraction.
- **Not provider-specific.** Configs and runtime MUST be provider-agnostic (Anthropic, OpenAI, Bedrock, Azure, Vertex, Mistral, etc.) because real client systems have data-residency constraints and existing LLM contracts.

## North star

> A Lead AI engineer should be able to describe a multi-agent system in ~5 minutes of typed-or-spoken prompt, iterate with the meta-agent until the eval harness passes a threshold (~30 minutes), review the resulting configs and prompts (~5 minutes), and commit the result to a git-tracked, rollback-able artifact that can be deployed into any supported runtime.

Everything in this doc tree is in service of that sentence.

## Primary persona

**Sam — Lead AI engineer, FDE-leaning.**
- Owns the architecture for AI systems that integrate into existing enterprise stacks.
- Deep Python, async, Pydantic, LLM-native.
- Values leverage over abstraction purity — chooses frameworks based on how much hand-written glue they remove.
- Reads git diffs, writes YAML, lives in a terminal and an IDE; does not want another SaaS dashboard unless it earns its keep.
- Needs reproducibility: every deployed agent must trace back to a commit, a config version, and a passing eval run.

## Secondary personas (design for, don't optimize for yet)

- **A collaborating engineer** auditing or extending a configured agent system. Must be able to read the configs and run the evals without prior foundry knowledge.
- **A future operator** (not the author) redeploying a system into a new environment. Needs deterministic config → deploy steps.

## Success criteria (v1)

The v1 release is complete when:

1. **Define → validate → run loop works.** A YAML agent config is loaded, validated by Pydantic, compiled into a LangGraph `StateGraph`, and executed end-to-end asynchronously against any configured provider.
2. **Eval harness runs.** A YAML eval set produces a scored run artifact with per-case pass/fail, metrics, and failure snippets.
3. **Meta-agent can configure an agent end-to-end.** Given a description and an eval set, it generates configs, runs the eval, reads failures, iterates prompt/config, versions each change in git, and stops at threshold or max iterations.
4. **Rollback is trustworthy — at multiple granularities.** Every meta-agent change is a git commit on a tracked branch. Rollback works at three levels: (a) per-tool (`foundry rollback <project> --tool <name> --to <version>` updates the version pin in `system.yaml`), (b) per-prompt (`--prompt` updates the pin in `agent.yaml`), and (c) per-project (`foundry rollback <project> --to <commit>` restores the whole tree). Each level is atomic; a failed rollback leaves the working tree unchanged.
5. **Multi-agent system works.** Supervisor + worker pattern implemented, with per-node state visibility configured in YAML and enforced at compile time.
6. **Reusable tool catalog.** Tools that are broadly useful (data-system queries, messaging, escalation) live in `catalog/` and are pinned by version in each project that uses them. New projects can enumerate catalog tools via the meta-agent; project-local tools can be promoted to the catalog with a human-gated `foundry catalog promote` command.
6a. **Standardised enterprise auth via shared Connections.** Every external-system integration (Snowflake, Postgres, Slack, S3, Salesforce, Jira, …) is a versioned `Connection` artifact in `catalog/connections/`. Tools declare connection *slots*; projects bind slots to specific versioned connections. The auth scheme (API key, OAuth2, SigV4, mTLS, JWT bearer, …) is the connection's, not the tool's. Credentials never appear in YAML — they're resolved via `SecretsProvider` + `CredentialsRef`. Connection pooling, token refresh, and health checks are built in. Rolling out a new auth scheme (e.g. OAuth → mTLS) is a `catalog/connections/<name>/v2/` + project pin upgrade; tool code doesn't change.
7. **Eval-driven iteration is real and comparable.** Each tool, agent, and project carries its own eval set. `foundry eval compare` reports version-over-version deltas for any artifact, and pin-set-over-pin-set deltas for whole projects (the "version bisect" workflow).
8. **API layer.** A configured project is exposed as a FastAPI app with one command (`foundry serve <project>`), with streaming support.
9. **Provider-agnosticism is real.** The same agent config runs against Anthropic, OpenAI, and at least one Bedrock- or Azure-hosted model without code changes.
10. **Observability as audit trail.** Every run emits structured events (per LLM call, per tool call, per handoff, per state transition) with dimensions suitable for aggregation — provider, model, tokens, latency, cost, tool version, success, error category. Transport is OTel; a local SQLite mirror supports cross-run queries. Designed so that monitoring dashboards for a deployed project can be built on the same stream with no instrumentation changes.

## Non-goals (v1)

Explicit non-goals prevent scope creep:

- No GUI agent-builder. The meta-agent is CLI + file-editing. A lightweight *review* UI is in scope (see `52-rollback-and-audit.md`, `82-dev-ux.md`), but not a drag-and-drop builder.
- No browser automation / computer use in v1. Tools are API-shaped.
- No fine-tuning. The foundry is purely prompt- and config-driven.
- No multi-tenancy, no auth, no RBAC inside the foundry. These are integration concerns for the target system.
- No distributed execution. Single-process async is fine for v1; LangGraph's checkpointer gives us resumability without needing a cluster.
- No "agent marketplace" / sharing configs between users.

## Guiding principles

These principles resolve ambiguous decisions. When two designs are both reasonable, the one more aligned with these principles wins.

### 1. Configs are text. LLMs edit text.

Every agent, tool, state shape, eval set, and orchestration graph is defined in a text file (YAML + markdown prompts + optional Python for tool bodies). Anything the meta-agent needs to change MUST be representable as a file edit. No "invisible" state in a database-only form. This is the seed idea from the meta-agent-configurator; it extends to every layer here.

### 2. Pydantic v2 is the validation layer for the whole stack.

Every config file is parsed into a Pydantic model on load. No untyped dicts leak past the loader. Runtime I/O (tool inputs, tool outputs, agent outputs) is Pydantic-validated at the boundary. This gives us: error messages that say what's wrong and where, JSON-schema generation for free, and a single source of truth for what a valid config looks like.

### 3. Provider-agnostic from day 1.

No agent code imports a specific provider SDK directly. All LLM calls go through the provider abstraction (see `11-provider-abstraction.md`). Provider-specific features (Anthropic cache control, OpenAI structured outputs, extended thinking) are exposed through a typed capabilities interface, not leaked via kwargs into agent code.

### 4. Async all the way down.

All I/O is `async def`. No sync agent entry points in v1. Blocking code inside an async boundary is a bug. The event loop belongs to the caller (CLI, FastAPI, notebook); the foundry never calls `asyncio.run` internally except at the top-level CLI entry.

### 5. Everything is versioned. Rollback is a first-class verb.

Every artifact the foundry writes — config, prompt, tool definition, agent definition, eval set, run artifact — has a version. The versioning backbone is git (see `51-git-backbone.md`). Rollback is atomic across multi-file changes. A rollback that only partially succeeds MUST fail loud and leave the working tree in a recoverable state.

### 6. State-visibility is config, not code.

When an agent is part of a multi-agent system, what state it can read and write is declared in YAML and enforced at compile time by the orchestration layer. Hand-written graphs that sidestep this enforcement are prohibited.

### 7. Eval-driven iteration is the default.

No agent ships without an eval set. The meta-agent's loop is `generate → eval → read failures → rewrite → re-eval`. Iterations stop at a threshold or max count. Eval runs are reproducible: same model + same config + same eval set + same seed → same (or nearly-same) score.

### 8. Two separate systems: runtime and configurator.

The runtime (the agents you've built) and the configurator (the meta-agent that built them) share abstractions but run in separate contexts. The configurator is a dev tool; it cannot accidentally affect a running production agent because it only writes config files — it does not touch live systems. This boundary is load-bearing and preserved throughout the architecture.

### 9. Observability is built in, not bolted on. Audit trails are first-class; metrics are derived from them.

Every agent run emits, for every LLM call, tool call, handoff, and state transition, a structured event with the dimensions you'd want for aggregation later — not just text for humans to read.

**Per LLM call:** provider, model, prompt tokens, completion tokens, cached-read tokens, latency, cost estimate, temperature, tool schemas presented, stop reason.
**Per tool call:** tool ref + version, input, output, success flag, latency, retry count, error category on failure.
**Per handoff:** from agent, to agent, trigger (rule/LLM/end), hop number, current state size.
**Per run:** run id, project, system version (git sha + pin-set hash), total duration, total cost, final status.

Transport is OpenTelemetry (traces + metrics); a local SQLite store mirrors the stream for queryable dev-time analysis. External backends (LangSmith, Langfuse, Datadog, Prometheus, etc.) plug in as OTel exporters.

The goal beyond debugging: the foundry is the *source* of monitoring metrics for deployed agents. When a project ships, its runtime audit stream feeds operational dashboards — latency per model, cost per project, tool failure rates, handoff anomalies. Building the audit-trail shape right in v1 means monitoring is a configuration exercise later, not a rewrite.

Not optional. The CLI shows the run ID at the end of every invocation so the user can grep for it later.

### 10. Small, orthogonal abstractions.

If two concepts can be separated, they are separated. A tool does not know about an agent; an agent does not know about a graph; a graph does not know about evals. Each layer consumes the layer below via a narrow interface.

### 11. Tools get typed, authenticated clients — not raw credentials.

Tool handlers request connections by *slot name* via `ctx.connections.get(slot)` and receive a live, pooled, authenticated client. They never touch secrets, never run the auth flow, never manage token refresh. Auth logic lives in the `Connection` artifact; pool/refresh/health live in the runtime. A tool that a human wrote pre-2026 with inline `requests.post(..., auth=...)` code is a smell — it should be a catalog connection + a thin tool that uses it.

## Relationship to LangGraph

The foundry is built on LangGraph as its execution runtime (see `02-framework-evaluation.md` for the decision). LangGraph provides: graph-structured execution, checkpointing, human-in-the-loop interrupts, parallelism primitives, observability hooks. The foundry provides: declarative config, Pydantic validation, provider abstraction, eval harness, versioning, meta-agent, API layer, state-visibility enforcement.

LangGraph's ownership boundary is "how does an agent execute." The foundry's ownership boundary is "how is an agent defined, validated, versioned, tested, and deployed." Where the boundary is fuzzy (e.g. state shape is both a LangGraph concept and a foundry concept), the foundry's Pydantic definition is the source of truth and the LangGraph `StateGraph` is compiled from it.

## Seed → full scope traceability

For each idea in `personal_docs/meta-agent-configurator.jsx`, here is where it lands in this doc tree:

| Seed concept | Expanded location |
|---|---|
| "Configs are text" | `00-vision-and-scope.md` (principle 1) |
| YAML config + markdown prompt | `12-config-and-validation.md` |
| Pydantic output schema | `11-provider-abstraction.md`, `21-agent-system.md` |
| Eval JSON | `40-eval-harness.md` |
| Meta-agent with file + eval tools | `60-meta-agent.md`, `61-meta-tools.md` |
| Auto-versioning (v1, v2, v3) | `50-versioning-model.md`, `51-git-backbone.md` |
| Iteration loop | `41-eval-driven-iteration.md` |
| Tool registry | `20-tool-system.md` |
| Agent registry | `21-agent-system.md` |
| Runtime vs configurator split | `00` principle 8, `01-architecture-overview.md` |
| Interactive session | `62-configurator-sessions.md` |
| **Shared tool catalog (beyond seed)** | `20-tool-system.md` (catalog layer), `50-versioning-model.md` (promotion semantics) |
| **Per-artifact versioning (beyond seed)** | `50-versioning-model.md` (three-axis model), `01-architecture-overview.md` (Versioning summary) |
| **Build-tool scaffold flow (beyond seed)** | `61-meta-tools.md` (`build_tool`, `build_agent`, `new_prompt_version`) |
| **End-to-end + per-artifact eval comparison (beyond seed)** | `40-eval-harness.md` (EvalComparison), `41-eval-driven-iteration.md` |
| **Shared Connections with pluggable auth schemes (beyond seed)** | `23-connections-and-auth.md`, `10-core-framework.md` (Connection protocol) |

## Decisions locked

| Decision | Locked on | Notes |
|---|---|---|
| Repo / package / CLI name | 2026-04-22 | `agent-foundry` (repo), `foundry` (package, CLI). Verb pairing `foundry forge` for meta-agent. |
| Core framework | 2026-04-22 | LangGraph. See `02-framework-evaluation.md`. |
| LLM providers | 2026-04-22 | Multi-provider from day 1 (Anthropic, OpenAI, + at least one cloud provider in v1). |
| Versioning model | 2026-04-22 | Three-axis: tools directory-versioned, prompts file-versioned, everything else git-versioned. |
| Catalog location | 2026-04-22 | Same repo as `src/`, sibling to `projects/`. Cross-user sharing not in v1 scope. |
| Python version | 2026-04-22 | 3.12 floor. |
| Package manager | 2026-04-22 | `uv`, lockfile committed. |
| Observability | 2026-04-22 | OTel always-on; structured audit trail for every LLM/tool/handoff; LangSmith opt-in. See principle 9. |

## Open questions for v1

1. **Catalog semver discipline.** Minor-version bumps for compatible changes, major-version bumps for schema-breaking changes. Does the foundry enforce this by comparing schemas across versions? Recommend: yes, warn (not block) on breaking changes when a promotion is attempted. TBD in `50-versioning-model.md`.
2. **Phase 6 forge demo task.** What concrete toy problem do we use to prove the meta-agent works end-to-end? Options: a numeric-QA agent against a MATH-lite-style eval, a text classification task, or a domain-adjacent task from FDE work. No commitment needed until Phase 6 starts.
3. **Licence.** Personal tool now, but worth noting if you ever open-source.
4. **Audit-trail storage retention.** Local SQLite grows unbounded. Default: truncate events older than N days (90?) but keep aggregated rollups indefinitely. TBD in `80-observability.md`.
