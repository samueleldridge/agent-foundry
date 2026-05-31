# 03 — Development Phases

## Purpose

This doc sequences the build into phases, each ending in a demo-able slice with explicit exit criteria. The goal is to always have a working foundry — every phase produces something you can actually run, not "infrastructure without a use case."

Phases correspond to doc tiers but are not 1:1 with them. Some tiers (e.g. Tier 8 — Ops) are consumed across multiple phases; some phases (e.g. Phase 3 — orchestration) pull from multiple tiers.

## Principles for phasing

- **Always runnable.** Every phase ends with a CLI command that produces real output. No "infrastructure phases" where the artifact is just code.
- **Eval-driven from Phase 4 onward.** Once the eval harness lands, every new capability ships with an eval that demonstrates it.
- **Versioning from Phase 5 onward.** Once git-backed versioning lands, every foundry-authored file change is a commit.
- **Skinny first, fat later.** Phases prefer vertical slices — a single-agent end-to-end path before multi-agent, a single provider before multi-provider polish, SQLite checkpointer before Postgres.
- **Each phase has an exit gate.** If the gate fails, we don't move on. The gate is a pytest marker set plus a manual smoke-test checklist.

## Phase map (at a glance)

| Phase | Title | Key deliverable | Depends on | Target duration |
|---|---|---|---|---|
| 0 | Decisions & skeleton | Repo layout, pinned deps, lint boundaries, empty but importable modules | — | 1–2 days |
| 1 | Core framework + providers + config | Trivial agent runs against Anthropic AND OpenAI from YAML | 0 | 4–6 days |
| 2a | Tools + connections + catalog + state visibility | One-tool agent runs end-to-end with pooled, authenticated connection; pin swap (tool or connection v1→v2) works | 1 | 5–6 days |
| 2b | Cache + embedders + retrieval | Semantic cache + hybrid retriever (RRF) + tool-result cache + rerankers run end-to-end on a second example project | 2a | 3–4 days |
| 2c | Memory + FunctionNode | Three memory layers (working/episodic/semantic) + FunctionNode in a flow + namespace and mixed-flow compile checks | 2b | 3–4 days |
| 3 | Single-agent orchestration on LangGraph | `foundry run` compiles a SystemSpec into a StateGraph and runs it | 2c | 3–5 days |
| 4 | Eval harness + per-artifact + comparison | `foundry eval` runs tool / agent / project evals; `compare` works across versions and pin-sets | 3 | 4–6 days |
| 5 | Versioning + git backbone + per-artifact rollback + catalog promote | Per-tool, per-prompt, and per-project rollback all atomic; `foundry catalog promote` gated | 4 | 4–5 days |
| 6 | Meta-agent | `foundry forge` iterates to threshold end-to-end on a toy task; uses `build_tool` + catalog + compare | 5 | 5–7 days |
| 7 | Multi-agent + HITL | Supervisor/worker with scoped state; interrupt/resume works | 6 | 4–6 days |
| 8 | API + async runtime polish | `foundry serve` exposes a system as FastAPI with SSE streaming | 7 | 3–5 days |
| 9 | Observability + dev UX + security + deploy | OTel traces; CLI polish; review UI skeleton; prompt-injection + tool allowlist guardrails; Dockerfile | 8 | 4–6 days |

Total calendar estimate (one engineer, focused): roughly 6–10 weeks. This is an estimate for architecture work with testing, not "AI-assisted hyper-speed."

---

## Phase 0 — Decisions & skeleton

### Deliverables

- `pyproject.toml` with pinned dependencies: `pydantic ^=2.*`, `langgraph ==<exact>`, `langchain-core ==<exact>`, `langchain-anthropic`, `langchain-openai`, `pyyaml`, `structlog` (or `loguru`), `typer` (or `click`), `anyio`, `httpx`, `opentelemetry-api`, `opentelemetry-sdk`, `opentelemetry-exporter-otlp`. Exact versions decided here and documented.
- `uv.lock` checked in. Package manager: **`uv`** (confirmed).
- `.python-version` set to **3.12** (confirmed).
- `src/foundry/` tree with empty `__init__.py` files matching the module layout in `01-architecture-overview.md` (including `catalog/`, `config/refs.py`, `versioning/artifacts.py`, `versioning/pins.py`, `configurator/tools/build.py`).
- Empty `catalog/` and `projects/` top-level directories committed with `.gitkeep` and a README per directory explaining purpose.
- `ruff.toml` with import-boundary rules enforcing the dependency diagram in `01`.
- `pytest.ini` / `pyproject.toml` tool section for test config and markers.
- `docs/` tree (already started).
- Minimal CLI: `python -m foundry --help` prints a help string.

### Exit gate

- [ ] `uv sync` succeeds on a clean clone.
- [ ] `python -m foundry --help` exits 0.
- [ ] `ruff check src/` passes.
- [ ] `pytest tests/` runs (even with zero tests) without import errors.
- [ ] Dependency versions and rationale committed to `docs/10-core-framework.md` (or placeholder pointing forward to Tier 1 writing).

### Deferred decisions logged here

- Artifact retention policy (answered in Tier 8).
- Secret-provider plug-point (answered in Tier 1 config doc).

---

## Phase 1 — Core framework + provider abstraction + config loader

### Deliverables

- **`foundry.core`** — `Agent` and `Tool` protocols; `Session` (incl. `CostBudget`); `FoundryMessage`; `ModelResponse` (incl. `TokenUsage` with `reasoning_tokens`); base exceptions (incl. `CostBudgetExceeded`).
- **`foundry.providers`** — `Provider` interface + `ProviderCapabilities` + concrete implementations for Anthropic and OpenAI at minimum (Bedrock, Azure, Vertex can follow). `ModelBinding` Pydantic model.
- **`foundry.config`** — YAML loader, Pydantic schemas for `AgentSpec`, `StateSpec`, `SystemSpec`, `ToolSpec` (stubs where needed for later phases), composition, secrets interface.
- **Runtime adapter** — the beginning of `foundry.runtime.langgraph_adapter`, enough to run a single-node graph.
- **CLI**: `foundry run <system-path> --input '...'` runs a trivial single-agent system that just calls the LLM.

### Exit gate

- [ ] `foundry run hello` produces a Greeting from Anthropic (`projects/hello/` exists).
- [ ] Change `model_binding.provider` to `openai`, same command runs with no other change.
- [ ] Change `model_binding.provider` to an unknown provider → structured error: "unknown provider 'foo'; available: anthropic, openai".
- [ ] `ruff` import-boundary check passes — no `langchain*` / `langgraph*` imports outside `foundry/runtime/`.
- [ ] YAML with invalid shape produces a Pydantic error message that identifies the file, the field, and why it's invalid.
- [ ] **Cost budget enforcement**: `Guardrails.max_cost_usd: 0.01` against a project that would consume more → `CostBudgetExceeded` raised pre-call; run terminates with `RunFailed`; budget context surfaced in audit trail.
- [ ] **TokenUsage.reasoning_tokens** populated when calling a reasoning-capable model (e.g. OpenAI o-series); zero when calling a non-reasoning model.
- [ ] Unit tests cover: provider lookup, capabilities introspection, config loading, env-var interpolation in YAML, secret path handling, cost-budget check + record.

---

## Phase 2 — overview

Phase 2 is delivered in three sub-phases (**2a → 2b → 2c**) on a strict dependency DAG. Each sub-phase is a vertical slice with its own hero demo, exit gate, AI review session, and manual smoke test. The split exists because the monolithic Phase 2 (~13 days, ~35 exit-gate items, 23 modules) is too large for one Claude Code session to hold without drift.

Cumulative duration matches the original estimate (10–14 days); the split exists for reviewability, not speed.

---

## Phase 2a — Tools + connections + catalog + state visibility

**Hero demo after this sub-phase**: a one-tool agent runs end-to-end — model → tool call (with pooled, authenticated connection) → tool result → model → final output. Catalog tool and catalog connection refs resolve. Pinning v1 → v2 (tool OR connection) in `system.yaml` makes the next run use the new version with no other change.

### Deliverables

- **`foundry.core.tool`** — `Tool` protocol (async handler returning a typed result), `ToolRegistry`.
- **`foundry.core.connection`** — `Connection` protocol, `ConnectionPool`, `ConnectionAccessor`, `ConnectionFactory`, `ConnectionHealth`, `ConnectionDescriptor`, `AuthScheme` enum.
- **`foundry.core.state`** — Pydantic-based state primitive with reducer annotations.
- **`foundry.config.schemas`** — `ToolSpec` (incl. `connections_required`; cache fields deferred to 2b), `AgentSpec` extensions (tool allowlist + state scope declarations; `semantic_cache` / `retrievers` / `memory` deferred to 2b/2c), `StateSpec`, `SystemSpec` (incl. tool + connection version pins), `ConnectionSpec`, `ConnectionBinding`, refresh/pool policies.
- **`foundry.config.refs`** — `ArtifactRef` parser + resolver handling tool and connection kinds (retriever and agent_template kinds added in 2b).
- **`foundry.catalog`** — catalog index loader, version discovery for tools and connections. No promotion yet (Phase 5).
- **`foundry.auth`** — 8 scheme helpers (api_key, basic_auth, oauth2_*, jwt_bearer, sigv4, mtls, custom), token cache, redactor.
- **`foundry.connections`** — `ConnectionPool` concrete impl, registry, health runner, descriptor builder.
- **`foundry.orchestration.state_scope`** — compile-time per-node visibility enforcement.
- **Per-tool and per-connection directory versioning on disk**. Each version immutable once committed.
- **Compile-time wiring validation** for connection slots (slot bound, slot `accepts` list matches bound ref).
- **Example catalog seeds**: `catalog/tools/` with 2–3 trivial shared tools; `catalog/connections/` with `postgres` + `pgvector` + `cohere_rerank` (the latter two are used by 2b but their connection shape lives here).
- **Updated `hello_agent`**: uses a catalog tool that declares a connection slot bound to a catalog connection.

### Exit gate

- [ ] A one-tool agent runs end-to-end: model → tool call (w/ connection acquired from pool) → tool result → model → final output.
- [ ] System resolves a catalog tool ref AND a catalog connection ref through the same code path.
- [ ] Changing a pin from `v1` → `v2` in `system.yaml` (tool OR connection) makes the next run use the new version, with no other change.
- [ ] Tool result is validated against the tool-version's output schema at the boundary; invalid output raises a structured error.
- [ ] Tool not in the agent's allowlist: registry refuses → error surfaces clearly.
- [ ] Tool whose slot is unbound in `system.yaml.connections` → compile-time `ConnectionSlotNotBoundError` with a clear message naming the missing slot.
- [ ] Tool slot's `accepts` list doesn't include the bound connection ref → compile-time error.
- [ ] Connection health check runs: `foundry connections health <project>/<name>` executes the connection's `health.yaml` eval.
- [ ] Connection pool caches: two tool calls in the same run sharing a connection slot reuse the same client instance (verified via pool metrics).
- [ ] Connection refresh: simulating a 401 with `refresh.mode: on_auth_error` evicts and rebuilds the connection on next acquire.
- [ ] Secret-literal scan catches a credential accidentally placed inside `SystemSpec.connections.*.config` → `ConfigLoadError`.
- [ ] State visibility: agent declared `read: [messages]` attempts to access `draft_plan` → compile-time `StateVisibilityError`.
- [ ] State reducers work: `append` concatenates lists; `merge` merges dicts; unannotated fields last-write-wins.
- [ ] Catalog index lists available tools and connections with their versions; missing version raises a structured error at compile time.
- [ ] Per-tool + per-connection version directory layout matches the spec; `versions.json` metadata present.
- [ ] Updated `hello_agent` system runs end-to-end against the catalog tool + connection.

---

## Phase 2b — Cache + embedders + retrieval

**Hero demo after this sub-phase**: a second example project (`rag_hello` or similar) — semantic cache hits on a re-run, semantic cache invalidates on a prompt-version bump, hybrid (dense + sparse) retriever runs in parallel and merges via RRF, reranker reorders.

### Deliverables

- **`foundry.core.embedder`** — `Embedder` protocol, `Embedding`, `EmbedderCapabilities`.
- **`foundry.core.retrieval`** — `Retriever`, `Reranker` protocols, `RetrievedDocument`.
- **`foundry.core.cache`** — `SemanticCache`, `ResultCache`, `CacheAccessor` protocols, key types.
- **`foundry.config.schemas`** — `ToolSpec` additions (`cacheable`, `cache_ttl_s`, `cache_scope`); `AgentSpec` additions (`semantic_cache: SemanticCacheConfig | None`, `retrievers: list[RetrieverBinding]`); `RetrieverBinding`, `RerankerBinding`, `SemanticCacheConfig`, `EmbedderBinding`.
- **`foundry.config.refs`** — extend resolver for retriever and agent_template kinds.
- **`foundry.catalog`** — extend version discovery for retrievers.
- **`foundry.providers.embedders`** — concrete adapters for Voyage, OpenAI, Cohere, Bedrock.
- **`foundry.cache`** — concrete `SemanticCache` + `ResultCache`: `in_process` (SQLite/FAISS), `redis` (Redis Stack), `pgvector` (Postgres pgvector).
- **`foundry.retrieval`** — concrete retrievers (`DenseRetriever`, `SparseRetriever`, `HybridRetriever` with RRF) and reranker adapters (Cohere, Voyage, Jina, local cross-encoder stub).
- **Compile-time wiring validation** for retriever bindings and cache backends (dimension match against embedder).
- **Catalog seeds**: `catalog/retrievers/pgvector_dense` + `catalog/retrievers/hybrid_rrf` templates.
- **Second example project** (`rag_hello` or similar) demonstrating semantic cache + hybrid retriever end-to-end.

### Exit gate

- [ ] **Embedder round-trip**: `EmbedderBinding` for Voyage `voyage-3` and OpenAI `text-embedding-3-small` both resolve and produce embeddings of advertised dimensions.
- [ ] **Semantic cache hit**: agent with `semantic_cache.backend: in_process` hits cache on the same input re-run; `cache.semantic.hit` event emitted with `similarity ≥ threshold`; `saved_cost_usd` populated.
- [ ] **Semantic cache invalidation**: bump a prompt version; same input now misses cache and emits `invalidate` event.
- [ ] **Tool-result cache**: tool with `cacheable: true` + `cache_ttl_s: 60` returns cached output on the second call in the same run; `cache.tool.hit` event emitted.
- [ ] **Tool-cache validator**: `cacheable: true` without `cache_ttl_s` → structured `ConfigValidationError` at load.
- [ ] **Cache failure fails open**: patched backend raises; run completes using LLM path + warning event; never blocks.
- [ ] **Hybrid retriever**: `hybrid_rrf` retriever calls dense + sparse in parallel, merges via RRF, returns top_k docs; `retrieval` event emitted; one-branch-fail-other-branch-return test passes.
- [ ] **Reranker**: `cohere_rerank` reorders input docs; `rerank` event emitted with `cost_estimate`.
- [ ] **Dimension mismatch compile check**: configuring a dense retriever whose embedder dimensions don't match the vector store's configured dimensions fails load with `EmbedderConfigError`.
- [ ] Second example project (`rag_hello` or equivalent) runs end-to-end with semantic cache + hybrid retriever.

---

## Phase 2c — Memory + FunctionNode

**Hero demo after this sub-phase**: a third example project — agent with three memory layers (working / episodic / semantic) runs; episodic layer retrieves top-K past snippets; semantic layer consolidates every N turns; a `FunctionNode` sits in a sequential flow with state visibility enforced.

### Deliverables

- **`foundry.core.node` + `foundry.core.function_node`** — `Node` protocol (parent of Agent + FunctionNode), `FunctionNode` protocol, `BaseFunctionNode`, `NodeResult`. Deterministic-Python flow nodes with same state-visibility / observability / retry plumbing as agents but no LLM.
- **`foundry.core.memory`** — `Memory`, `MemoryLayer` protocols, `MemoryEnvelope`, `MemoryContribution`, `MemoryWrite`, `MemoryContext`.
- **`foundry.config.schemas`** — `AgentSpec` addition (`memory: MemoryConfig | None`); `FunctionNodeSpec`.
- **`foundry.memory`** — `DefaultMemory` coordinator, three concrete layers (`WorkingMemoryLayer`, `EpisodicMemoryLayer`, `SemanticMemoryLayer`), prompt-assembly logic.
- **Remaining compile-time wiring validation**: namespace collisions (agent + function same name), mixed-flow `from`/`to` references resolve across agents and functions.
- **Third example project** demonstrating memory layers + a `FunctionNode` in a sequential flow.

### Exit gate

- [ ] **Memory: working layer**: configured with `max_messages: 5`; on a 10-turn run, agent prompt contains exactly the last 5 message turns from state.
- [ ] **Memory: episodic layer**: configured against a seeded retriever; agent's prompt includes top-K retrieved past snippets in the configured `system_suffix` placement; `memory.read` event lists `episodic` in `layers_read`.
- [ ] **Memory: semantic layer with periodic consolidation**: every N turns, the consolidator prompt runs and writes synthesised content into the configured state field; `memory.consolidate` event emitted with input/output token counts.
- [ ] **Memory: degrade-gracefully (default)**: a failed retriever in episodic layer → contribution empty + warning event; run completes.
- [ ] **Memory: fail-strict mode**: same failure → `MemoryLayerError` raised, run aborted.
- [ ] **Memory: envelope token cap**: configured `max_envelope_tokens` triggers truncation of last-listed layer first; `truncated: true` flag in event.
- [ ] **Memory: layer-name uniqueness**: two layers with the same name → `ConfigValidationError` at load.
- [ ] **FunctionNode end-to-end**: a `sequential` flow `[normalize_input_function, hello_agent, format_output_function]` runs; both function nodes execute their Python; agent runs in between; final state reflects the full pipeline.
- [ ] **FunctionNode state visibility**: function with `read: [a, b], write: [c]` returning `{a: ..., c: ...}` → only `c` is written; `a` is dropped + warning event.
- [ ] **FunctionNode observability**: `function_node.started` and `function_node.completed` events emitted with `node_name`, `node_version`, `fields_written`, `bytes_delta`, `latency_ms`.
- [ ] **Node namespace collision**: an agent and function with the same name → `CompileError` at load.
- [ ] **Mixed flow validation**: a `graph` flow's `from`/`to` references resolve to either agents or functions interchangeably; missing reference → `CompileError`.

---

## Phase 3 — Single-agent orchestration on LangGraph

### Deliverables

- **`foundry.orchestration.compiler`** — `SystemSpec` → `CompiledSystem`. Single-agent pattern first. Parallel/sequential/supervisor patterns stubbed.
- **`foundry.runtime.langgraph_adapter`** — full single-agent flow: `StateGraph` construction, async node, checkpointer wiring, streaming.
- **`foundry.runtime.checkpointers`** — in-memory checkpointer for tests; SQLite checkpointer for dev.
- **CLI**: `foundry run` supports `--stream` and `--checkpoint sqlite|memory|none`.

### Exit gate

- [ ] A single-agent system runs end-to-end through LangGraph with a checkpointer attached.
- [ ] Kill the process mid-run (simulate by raising after N tool calls). Start a new process with the same run id. Execution resumes and completes.
- [ ] Streaming: `foundry run --stream` emits incremental output.
- [ ] Trace spans include the run id, the system name, and the agent name.
- [ ] Adapter module is the only importer of `langgraph` in the codebase (lint check passes).

---

## Phase 4 — Eval harness + per-artifact + version comparison

### Deliverables

- **`foundry.eval.schemas`** — `EvalSpec`, `EvalCase`, `EvalScorer`, `EvalRunResult`, `EvalComparison`.
- **`foundry.eval.harness`** — async runner that: loads a system (or a tool or agent) + an eval set, runs each case, scores outputs, aggregates, writes artifact. Handles three granularities:
  1. **Tool-level**: runs a specific tool version against its `eval.yaml`. Cases are direct input → expected output; no agent in the loop.
  2. **Agent-level**: runs a specific agent in isolation against an agent-scoped eval set.
  3. **Project-level**: runs the full compiled system against an end-to-end eval set.
- **`foundry.eval.compare`** — cross-version comparison: runs the same eval against multiple artifact versions (or multiple pin-sets at the project level) and produces an `EvalComparison` artifact with per-case deltas.
- **`foundry.eval.scorers`** — `exact`, `llm_judge`, `rubric` implementations.
- **`foundry.eval.reporter`** — CLI and machine-readable (JSON) formats; comparison tables for `compare`.
- **CLI**:
  - `foundry eval <project> <eval-set>` — run end-to-end.
  - `foundry eval tool <ref>@<version>` — run a tool's standalone eval.
  - `foundry eval agent <project> <agent>` — run an agent's eval.
  - `foundry eval compare --tool <name> <v1> <v2> ...` — cross-version tool comparison.
  - `foundry eval compare --project <name> --pin-set <ref1> --pin-set <ref2>` — cross-pin-set project comparison.

### Exit gate

- [ ] A hello-world project eval set with 5 cases runs and produces a result with a score and per-case details.
- [ ] A hello-world tool eval runs against a catalog tool at `v1` and at `v2`; `foundry eval compare --tool <name> v1 v2` produces a side-by-side report.
- [ ] End-to-end comparison across two pin-sets (current vs a prior commit) runs and reports per-agent deltas.
- [ ] `llm_judge` scorer uses the provider abstraction — it is not hardcoded to a provider.
- [ ] Eval run artifact is stored under `~/.foundry/runs/<eval_run_id>/` and is readable by `foundry.eval` utilities (required by Phase 6).
- [ ] Determinism: same system + same eval set + same seed → same score (within tolerance allowed by scorer type). Documented in eval schema.
- [ ] `foundry eval --fail-under 0.9` returns non-zero if score < 0.9 (for CI use).

---

## Phase 5 — Versioning + git backbone + per-artifact rollback + catalog promotion

### Deliverables

- **`foundry.versioning.git_backend`** — thin wrapper around `git` (subprocess, not a Python git library, for predictability and because meta-agent will also shell out). Functions: `ensure_branch`, `commit`, `log`, `show`, `revert`, `checkout_paths`.
- **`foundry.versioning.artifacts`** — per-artifact version I/O: create next version directory for a tool, create next prompt file for an agent, list versions, read `versions.json` metadata.
- **`foundry.versioning.pins`** — read/write helpers for tool version pins in `system.yaml` and prompt version pins in `agent.yaml`. Transactional (all-or-nothing) across multiple pin edits.
- **`foundry.versioning.rollback`** — three rollback modes:
  1. **Per-tool**: update the pin in `system.yaml` to an earlier version. Commit.
  2. **Per-prompt**: update the pin in `agent.yaml`. Commit.
  3. **Per-project (coarse)**: `git checkout <commit> -- projects/<name>/` + commit. Atomic across all files.
- **`foundry.catalog.promote`** — human-gated `foundry catalog promote <project>/<kind>/<name>` — copies a project-local tool OR connection's latest version to the corresponding catalog path, updates the catalog index, commits. Refuses if the artifact's eval (tool's standalone eval or connection's health check) doesn't meet a configurable minimum score.
- **`foundry.versioning.audit`** — append-only `.foundry/audit.jsonl` per project.
- **`foundry.versioning.refs`** — `ArtifactRef` resolution against on-disk directory structure.
- **CLI**: `foundry rollback <project> [--tool <name> --to <ver>] [--prompt <agent> --to <ver>] [--to <commit>]`; `foundry versions <project> [--tool <name>]`; `foundry diff <project> <ref1> <ref2>`; `foundry catalog promote <project>/<tool>`.

### Exit gate

- [ ] **Per-tool rollback**: `foundry rollback pipeline_recon --tool validate_deltas --to v2` updates only the pin in `system.yaml`; `git diff HEAD~1` shows a single-file change; no other tools or agents touched.
- [ ] **Per-prompt rollback**: analogous for a prompt pin in `agent.yaml`.
- [ ] **Per-project rollback**: `foundry rollback pipeline_recon --to HEAD~5` restores the whole project subtree atomically — all files or none.
- [ ] Rollback refuses to operate if working tree has uncommitted changes to the project (unless `--force`).
- [ ] Catalog promotion copies a local tool's files to `catalog/tools/<name>/v<N>/`, refuses to overwrite an existing catalog version, updates the catalog index, and commits.
- [ ] Catalog promotion is blocked when the tool's standalone eval score is below the configured floor.
- [ ] Audit log records each versioning/promotion operation with run id, commit sha, artifact affected, and operator (meta-agent vs. human).
- [ ] Rolling a tool back to a version whose input/output schema is incompatible with a consuming agent produces a compile-time error on the next run; the rollback itself succeeds but surfaces the incompatibility.

---

## Phase 6 — Meta-agent

### Deliverables

- **`foundry.configurator.meta_agent`** — `MetaAgent` class, itself a `foundry.Agent` (single-agent, on LangGraph, with a checkpointer). Its prompt and tools are fixed in this release.
- **`foundry.configurator.tools`** — the full meta-toolkit:
  - Filesystem: `read_file`, `write_file` (sandboxed to the scoped project).
  - Discovery: `list_catalog`, `list_tools`, `list_agents`.
  - Scaffolds: `build_tool` (creates next tool version directory with the 5-file shape), `build_agent` (creates agent directory with agent.yaml + v1 prompt + output_schema.py), `new_prompt_version` (copies the live prompt to `v<N+1>.md`).
  - Pinning: `pin_version` (updates a tool pin in system.yaml or a prompt pin in agent.yaml).
  - Eval: `run_eval` (per-tool, per-agent, or project), `read_eval_results`, `compare_versions`.
  - Versioning: `git_commit`, `git_show`, `list_versions`, `rollback` (per-tool / per-prompt).
- **`foundry.configurator.session`** — the iteration loop: receive description + eval set → discover catalog + local → design system → scaffold missing tools → generate agent configs → eval → read failures → rewrite prompts or bump tool versions → commit → re-eval → stop at threshold or max.
- **Meta-agent prompt (`foundry/configurator/prompts/v1.md`)** — includes explicit guidance on: prefer catalog tools over building new; only `build_tool` for genuinely project-specific tools; use `compare_versions` to decide whether a new tool/prompt version is actually an improvement before pinning; call `rollback` if an iteration regressed.
- **CLI**: `foundry project new <name>`, `foundry forge <project> --description "..." --eval <path> --threshold 0.9 --max-iter 5`.

### Exit gate

- [ ] On a toy problem (e.g. "build a numeric-answer QA agent against MATH-lite eval"), `foundry forge` completes, produces a working agent, and logs an improvement trajectory across ≥2 iterations.
- [ ] Meta-agent uses at least one catalog tool in its solution (demonstrates discovery + pinning).
- [ ] Meta-agent scaffolds at least one project-local tool via `build_tool`, runs its standalone eval, and iterates the handler until the tool eval passes before wiring it into the system.
- [ ] Each meta-agent iteration is a distinct commit on the project's branch; commit messages reference the artifact affected.
- [ ] Failure to meet threshold after max iterations exits with a clear "best effort" state; the user can inspect the final commits and continue manually.
- [ ] Meta-agent's `write_file` is sandboxed: attempting to write outside the scoped project (including any write to `catalog/` or `src/foundry/`) raises and aborts the run.
- [ ] Meta-agent can rollback a bad iteration using per-artifact rollback: demonstrated by forcing a regression in a prompt, watching the meta-agent detect it via `compare_versions`, and revert the pin.

---

## Phase 7 — Multi-agent orchestration + HITL

### Deliverables

- **`foundry.orchestration.patterns`** — supervisor, sequential, parallel, router implementations, each as a reusable compilation helper.
- **Full compiler support for `flow` config.** The compiler handles nested flows (a supervisor containing a parallel group, etc.).
- **`foundry.orchestration.hitl`** — `ApprovalRequired` exception, wiring to LangGraph `interrupt()`, resume path.
- **CLI**: `foundry resume <run_id>` with `--approve` / `--reject --reason "..."`.
- Full state visibility enforcement across multi-agent graphs (subgraph-per-agent with scoped schemas).

### Exit gate

- [ ] A supervisor + 2 workers system runs end-to-end.
- [ ] Workers cannot read fields outside their visibility — demonstrated with an assertion in an integration test.
- [ ] Parallel fan-out + fan-in works; results from parallel workers merge into state correctly (reducer semantics tested).
- [ ] HITL interrupt: a tool-call approval is required mid-run; the process pauses; CLI shows pending approval; `foundry resume --approve` continues; the final output reflects the approval.
- [ ] Kill + resume still works in multi-agent runs (checkpointer survives).

---

## Phase 8 — API layer + end-to-end streaming + scaling + async runtime polish

### Deliverables

- **`foundry.api.app`** — FastAPI app factory, per-project endpoint generation (introspected from `SystemSpec`: input shape from terminal agent's state-write fields + project input schema, output shape from terminal agent's `output_schema`). Auth plug-point (bearer stub), CORS stub.
- **`foundry.api.streaming`** — SSE encoder for `POST /stream` with `Last-Event-ID` resume via persisted `RunEvent` replay; WebSocket handler for `WS /ws` bidirectional (outbound `RunEvent`, inbound `InboundMessage`: `InjectInput`, `ApprovalResponse`, `CancelRun`, `PauseRun`, `ResumeRun`).
- **`foundry.api.routes`** — `POST /run`, `POST /stream`, `POST /batch`, `WS /ws`, `GET /runs/{run_id}`, `GET /runs/{run_id}/events?from_sequence=N`, `POST /runs/{run_id}/resume`, `GET /health`, `GET /config`.
- **`foundry.api.batch`** — batch submission primitive: accepts a list of inputs, streams per-item `RunEvent`s tagged with `batch_id` + `item_id`, enforces batch-level cost budget. Full spec in `85-batch-and-throughput.md`.
- **`foundry.api.worker`** — worker identity (`worker_id` = hostname+pid); tagged into every `RunEvent` and metric.
- **Multi-worker prod shape**: uvicorn `--workers N`; Postgres checkpointer + Redis rate limiter documented as the prod configuration. Sticky `run_id → worker` for WebSocket (LB hash or Redis registry); SSE resume is worker-agnostic.
- **`foundry.providers` rate limiter swap**: `FOUNDRY_RATE_LIMITER=redis://...` activates the Redis-backed `TokenBucket`; default remains in-process.
- **Cancellation / timeout polish**: client-side disconnect or explicit timeout propagates into LangGraph; checkpoint persists; `run.cancelled` event emitted with a reason.
- **CLI**: `foundry serve <project> --host ... --port ... --workers N`.

### Exit gate

- [ ] `foundry serve hello` + `curl` round-trip produces a Greeting via `POST /run`.
- [ ] The OpenAPI schema generated for a project matches its `SystemSpec` input/output Pydantic shapes (no hand-written routes per project).
- [ ] SSE streaming: `POST /stream` emits progressive `RunEvent`s (`run.started` → `llm.delta` × N → `run.completed`); connection closes cleanly.
- [ ] SSE reconnect: kill client mid-stream, reconnect with `Last-Event-ID: <N>`; server replays from sequence `N+1` using the persisted run artifact.
- [ ] WebSocket round-trip: connect, send `InjectInput`, observe it reflected in subsequent `LLMDelta`; send `CancelRun`, observe `run.cancelled`.
- [ ] WebSocket HITL: a tool raises `ApprovalRequired`; client receives `approval.required` event; sends `ApprovalResponse`; `approval.resolved` emitted; run resumes to `run.completed`.
- [ ] Kill client mid-stream → server cancels run → `GET /runs/{run_id}` shows status; `POST /runs/{run_id}/resume` succeeds.
- [ ] Batch: `POST /batch` with 20 inputs; per-item `RunEvent`s stream over one SSE connection with correct `batch_id`/`item_id` tagging; batch-level cost budget enforced.
- [ ] Rate limiter (prod): 3 workers share a Redis-backed token bucket on `anthropic:claude-opus-4-7`; aggregate call rate stays under the configured limit under synthetic load.
- [ ] **Sustained load test**: 100 concurrent runs/sec for 5 minutes against a trivial project; 0 dropped events, p95 LLM latency within 2× of baseline, 0 orphan connections in the pool, batch-level cost budget enforced (over-budget runs fast-fail cleanly).

---

## Phase 9 — Observability + dev UX + security + deploy

### Deliverables

- **`foundry.observability.tracing`** — OTel setup finalised; spans for `foundry.run`, `foundry.node`, `foundry.llm`, `foundry.tool`, `foundry.handoff`, `foundry.state_transition`, `foundry.eval`. Attribute spec enforced (see `80-observability.md` / `01-architecture-overview.md` attribute table).
- **`foundry.observability.metrics`** — OTel metrics: counters + histograms + gauges for LLM calls, tool calls, handoffs, eval runs. Tagged for aggregation.
- **`foundry.observability.store`** — local SQLite event-mirror at `~/.foundry/observability.db`. Schema: `runs`, `llm_calls`, `tool_calls`, `handoffs`, `evals`.
- **`foundry.cli.obs`** — CLI query surface: `foundry obs cost --project <name> --since 7d`, `foundry obs tool-failures --tool <name>`, `foundry obs p95 --model <name>`. Each emits a table.
- **Optional LangSmith exporter** (`FOUNDRY_TRACING=langsmith`).
- **`foundry.storage`** — artifact store layout finalised; retention: raw events truncated after 90 days (configurable), aggregated rollups retained indefinitely.
- **`foundry.cli.tui`** — minimal "review UI" skeleton: a textual/TUI page that shows project branch commits, per-artifact version lists, eval trajectories (per-artifact and project), and a "rollback" action. Not a full UI — the minimum to make rollback trustworthy without remembering git incantations.
- **`foundry.security`** — tool sandbox module (allowlist + path restriction for meta-agent fs tools), prompt-injection guardrails around tool output interpolation, input/output validators.
- **Deploy**: Dockerfile for the API server; example env-var manifest; secrets-provider pluggable.

### Exit gate

- [ ] Every run produces OTel traces with all mandatory attributes from the attribute spec. Schema drift is caught by a contract test.
- [ ] Every run produces OTel metrics that aggregate cleanly (can compute "total cost for project X last 7 days" directly from the metric store).
- [ ] Local SQLite store captures the same events; `foundry obs` commands return correct results that match the OTel stream.
- [ ] `foundry obs cost --project hello --since 1d` produces a usable cost breakdown.
- [ ] Every run still produces a `RunArtifact` with complete metadata, inputs, outputs, state transitions, plus the llm_calls and tool_calls JSONL streams.
- [ ] Review TUI lists commits for a project, shows per-artifact version lists with eval scores, diffs, and invokes per-artifact rollback.
- [ ] Security: inject a tool whose output contains an obvious prompt-injection pattern ("Ignore previous instructions..."); the tool-output interpolation surrounds it with a typed boundary that the agent prompt references explicitly. Documented in `83`.
- [ ] `docker build` + `docker run foundry-api` serves a configured project end-to-end with OTel exporting to a container-side collector.
- [ ] A top-to-bottom demo runs in ≤5 minutes of real time: forge a tiny project, eval it, serve it, hit the API, roll back a deliberate regression, view cost metrics.

---

## Cross-cutting concerns per phase

These are the items that live in every phase's definition-of-done:

| Concern | What "done" looks like |
|---|---|
| Tests | Every new module has unit tests; every phase ends with integration tests for the end-to-end slice. |
| Docs | The docs tier for the phase is updated; examples in the doc reflect current code. |
| Type checking | `mypy --strict src/foundry/` passes. |
| Lint | `ruff check` passes; import-boundary rules honoured. |
| Secrets | No secrets in YAML or in tests (use env vars with fixtures). |
| Run id | Every execution path threads a run id through logs, traces, and artifacts. |

## Phase-gate rituals

At the end of each phase:

1. **Smoke test demo.** Run the phase's hero command from a clean checkout in a fresh venv. Record the output in `docs/_demos/phase-<N>.md`.
2. **Retrospective block.** A paragraph in `docs/_retros/phase-<N>.md` — what took longer than expected, what changed from the plan, what the phase+1 needs to watch.
3. **Memory write.** Update project memory with any design decisions that resolved during the phase, so future sessions inherit them.

Retros and demos are cheap; they are the glue that keeps the plan honest.

## Risk register

Known risks, how we'll see them, and a response:

| Risk | Signal | Response |
|---|---|---|
| LangGraph minor release breaks the adapter | Contract tests fail on upgrade | Revert version pin, fix adapter on a branch |
| PydanticAI looks too tempting during Phase 2 | Frequent "if only" moments writing state code | Capture cost in retro; do not switch mid-phase |
| Meta-agent cost blows up | Forge runs cost >$X for a trivial task | Add a cost budget to `forge`, hard-cap tokens |
| Git rollback corrupts config in edge cases | An integration test fails post-rollback | Halt Phase 5 exit gate until fixed — non-negotiable |
| State visibility enforcement has false negatives | A worker reads forbidden fields and nothing complains | Dedicated fuzz test in Phase 7 |
| Provider feature drift (e.g. Anthropic ships a new kwarg) | User asks for it, Provider interface doesn't expose it | Capabilities interface is designed to extend — add a new capability flag + surface in `ModelSettings` |

## Open questions

- **Demo problem for Phase 6 forge gate.** What toy task do we run the meta-agent on to prove it works? Candidate: a numeric-answer QA agent against a small synthetic eval set. Decide before Phase 6 starts.
- **CI.** Do we run the full eval harness in CI? Proposal: yes, on a deterministic eval set with a fixed seed, so regressions are caught. Discuss during Phase 4.
- **Bedrock / Azure support.** These typically go in Phase 1 stub, Phase 9 polish. Firm up timing with any near-term client needs.
