# 90 — Implementation Plan

## Purpose

This doc is the operator's playbook for implementing `agent-foundry` from the design specs. It mirrors the ten phases in `03-development-phases.md` (Phases 0–9) and adds the implementation workflow: **paste-ready prompts for fresh Claude Code sessions per phase**, **explicit stop gates + commit checkpoints**, and **review prompts to verify each phase from a fresh session**.

The point of the fresh-session pattern: a single Claude Code session implementing all 10 phases would accumulate context bloat + drift. Each phase gets its own session with a focused prompt; the session ends at the stop gate; a separate review session validates before moving on. This pattern keeps each session sharp + makes the work auditable phase by phase.

## How to use this doc

For each phase below:

1. **Read the phase summary** in `03-development-phases.md` first (the source of truth for what to build).
2. **Open a fresh Claude Code session** in the repo.
3. **Paste the implementation prompt** from this doc.
4. **Let Claude Code work** to the documented stop gate. Don't paste new directions mid-phase — that's what causes drift.
5. **At the stop gate**, the session commits its work + writes a **handoff note** under `docs/_phase_handoffs/phase_<N>.md` summarising what was done.
6. **Open a second fresh Claude Code session** for review.
7. **Paste the review prompt** from this doc.
8. **Review session reports pass / partial / fail** against the phase exit gate from `03-development-phases.md`.
9. **If pass**: move to phase N+1.
10. **If partial / fail**: open a third fresh session with a follow-up prompt naming the specific gaps; re-review.

Each phase has three prompts:
- **Implementation prompt** (paste into the implementation session).
- **Stop-gate prompt** (sometimes; for phases where the stop is a complex check).
- **Review prompt** (paste into the review session).

## Repo invariants every session honours

These apply across every implementation session:

- **Conventional commits with no Claude co-author line** (per memory; `feat:` / `fix:` / `chore:` / `docs:` / `refactor:` / `test:` / `ci:` / `build:` / `perf:` / `style:`).
- **No institution-name leakage in repo content** (no Citadel / specific firm names; generic placeholders).
- **Persona is "AI engineer", not "Lead AI engineer"** in any repo docs.
- **Branch model**: implementation work happens on `main` for the framework code (consistent with the existing repo pattern); per-project work happens on `foundry/<project>` branches once projects exist.
- **Test discipline**: every phase ends with passing CI gates per `03-development-phases.md`'s exit criteria.
- **Doc reference discipline**: implementation prompts must point sessions at specific doc paths to read; never assume the session has the spec memorised.

## Phase 0 — Decisions & skeleton

**Source of truth**: `docs/03-development-phases.md` § Phase 0.

**Goal**: repo skeleton that's importable, lint-clean, runs `python -m foundry --help`. No behaviour yet; just the structure that every later phase fills in.

### Implementation prompt for Phase 0

```
You are implementing Phase 0 of agent-foundry per docs/03-development-phases.md
§ Phase 0.

START FROM: this repo's current state (33 design docs in docs/; no
src/foundry/ code yet). Read these docs first to ground yourself:
- docs/00-vision-and-scope.md (frame)
- docs/01-architecture-overview.md (especially § Directory layout +
  § Module ownership and dependency rules)
- docs/03-development-phases.md § Phase 0 (full)
- docs/10-core-framework.md § Module boundaries (the import-boundary rules)

DELIVERABLES for Phase 0:
1. pyproject.toml with pinned dependencies as listed in
   03-development-phases.md § Phase 0 deliverables. Pick exact pins (you
   may consult uv search or the relevant package's release notes if
   needed). Document chosen versions in a comment in pyproject.toml.
2. uv.lock checked in (run `uv sync` after pyproject.toml is ready).
3. .python-version file with "3.12".
4. src/foundry/ tree with empty __init__.py files matching the module
   layout in docs/01-architecture-overview.md § Directory layout.
   Specifically include: core/, providers/, config/, catalog/, auth/,
   connections/, cache/, retrieval/, memory/, orchestration/, runtime/,
   eval/, versioning/, configurator/, api/, observability/, storage/,
   cli/, security/. Include the sub-files indicated in the doc (e.g.,
   core/agent.py, core/tool.py, etc.) as empty stubs that just contain
   placeholder module docstrings.
5. Empty top-level catalog/ and projects/ directories with .gitkeep +
   a README per directory explaining purpose (per
   03-development-phases.md § Phase 0).
6. ruff.toml with import-boundary rules enforcing the dependency
   diagram in docs/01-architecture-overview.md § Module ownership.
   Specifically: foundry.core MUST NOT import langgraph / langchain* /
   anthropic / openai / foundry.providers / foundry.runtime /
   foundry.config (rules per docs/10-core-framework.md § Enforcement).
7. pytest.ini or pyproject.toml [tool.pytest.ini_options] section with
   sensible defaults (testpaths, asyncio_mode = "auto", markers).
8. tests/unit/, tests/integration/, tests/contract/ directories with
   .gitkeep + a placeholder test_smoke.py in tests/unit/ that asserts
   `import foundry` works.
9. Minimal CLI: src/foundry/cli/__main__.py that responds to
   `python -m foundry --help` with a help string listing the planned
   subcommands (use argparse or typer; pick + document choice).

EXIT GATE (you must verify each before declaring done):
- [ ] `uv sync` succeeds on a clean clone (you simulate by deleting
      .venv + uv.lock and re-running)
- [ ] `python -m foundry --help` exits 0 and prints help
- [ ] `ruff check src/` passes (zero violations)
- [ ] `pytest tests/` runs (with the smoke test) and exits 0
- [ ] All 18 src/foundry/ subdirectories exist with __init__.py +
      module-docstring placeholders
- [ ] catalog/ and projects/ exist with .gitkeep + README

WHEN COMPLETE:
1. Write a handoff note to docs/_phase_handoffs/phase_0.md with:
   - Pinned dependency versions chosen + rationale
   - Any deviations from docs/03-development-phases.md (and why)
   - List of files created
   - Confirmation of each exit-gate check
2. Commit with conventional format:
   "feat(phase-0): repo skeleton with pinned deps + import-boundary lint"
3. STOP. Do not start Phase 1 work. End the session with a clear
   "Phase 0 complete; ready for review" message.

DO NOT:
- Implement any actual behaviour beyond the help string in the CLI.
- Write provider adapters, agent classes, etc. (those are Phase 1+).
- Skip the .python-version, ruff config, or test directories.
- Use a different branch — work on main.
- Add a Claude co-author line to commits.
```

### Review prompt for Phase 0

```
You are reviewing Phase 0 of agent-foundry's implementation per
docs/03-development-phases.md § Phase 0 exit gate. Read:
- docs/03-development-phases.md § Phase 0 (full)
- docs/_phase_handoffs/phase_0.md (the implementing session's handoff)
- The current state of the repo

VERIFY each exit gate:
1. Run `uv sync` on a hypothetical clean clone — does pyproject.toml +
   uv.lock support it? (You can simulate by checking the lock file is
   complete + the deps resolve consistently.)
2. Run `python -m foundry --help` — does it exit 0 with a help string?
3. Run `ruff check src/` — zero violations?
4. Run `pytest tests/` — passes (smoke test only at this phase)?
5. Are all 18 src/foundry/ subdirectories present with __init__.py?
6. Are catalog/ + projects/ present with .gitkeep + README?
7. Does ruff.toml enforce the import-boundary rules from
   docs/10-core-framework.md § Enforcement? Specifically: does it
   block langgraph / langchain* / anthropic / openai / foundry.providers
   / foundry.runtime / foundry.config from being imported in core/?

Report:
- For each exit-gate item: PASS / FAIL with specific evidence.
- Any deviations from spec (cite file path + line).
- Any concerns about pin choices (e.g., outdated or known-unstable
  versions).
- Overall verdict: PASS (move to Phase 1) / PARTIAL (specific gaps) /
  FAIL (significant rework needed).

Do NOT implement fixes; this is review only. If you identify gaps,
note them clearly so the next implementation session can address them.
```

---

## Phase 1 — Core framework + provider abstraction + config loader

**Source of truth**: `docs/03-development-phases.md` § Phase 1.

### Implementation prompt for Phase 1

```
You are implementing Phase 1 of agent-foundry. Phase 0 is complete.
Read these docs first to ground yourself:
- docs/03-development-phases.md § Phase 1 (full deliverables + exit gate)
- docs/10-core-framework.md (FULL — this is your primary spec)
- docs/11-provider-abstraction.md (FULL)
- docs/12-config-and-validation.md (FULL — Pydantic schemas are normative)
- docs/_phase_handoffs/phase_0.md (handoff context)

DELIVERABLES (per docs/03 § Phase 1):
1. foundry.core — Agent + Tool + Connection + Embedder + Retriever +
   Reranker + FunctionNode + Node protocols; BaseAgent +
   BaseFunctionNode; LifecycleHooks; Session + RunId + CancelToken +
   CheckpointerHandle + CostBudget; FoundryMessage + ContentBlock
   discriminated union; ModelResponse + ModelDelta + StopReason +
   TokenUsage (incl. reasoning_tokens); RunEvent tagged union;
   InboundMessage tagged union; StateBase + Reducer enum; full
   FoundryError exception hierarchy with to_dict() contract.
2. foundry.providers — Provider protocol + ProviderAdapter base class
   + Anthropic + OpenAI implementations (Bedrock + Azure + Vertex can
   stub for now). ModelBinding + ProviderCapabilities + ModelSettings.
   Capability-required compile-time check.
3. foundry.config — YAML loader with structured error reporting (file +
   pointer + line/column + received-vs-expected + Levenshtein hints);
   complete Pydantic schemas for SystemSpec / AgentSpec / StateSpec /
   ToolSpec (stubs OK for fields not yet used) / ConnectionSpec +
   ConnectionBinding / EvalSpec / FunctionNodeSpec; composition (extends
   one-deep + ${ENV:NAME:default} interpolation); secrets module with
   secret-literal scan.
4. Runtime adapter beginning — foundry/runtime/langgraph_adapter.py
   enough to compile + run a single-node graph against a single
   provider call. ONLY this file imports langgraph / langchain*.
5. CLI: `foundry run <project-path> --input '...'` runs a trivial
   single-agent system that just calls the LLM. Use the FunctionNode
   ALSO supported (per Tier 1 patches that added it).
6. Trivial example project under projects/hello/ with system.yaml +
   state.yaml + agents/hello_agent/{agent.yaml, prompts/v1.md,
   output_schema.py}.

EXIT GATE (per docs/03 § Phase 1):
- [ ] `foundry run hello` produces a Greeting from Anthropic
- [ ] Change model_binding.provider to openai → same command runs
      with no other change
- [ ] Change to unknown provider → structured error naming available
- [ ] ruff import-boundary lint passes; zero langchain/langgraph
      imports outside foundry/runtime/langgraph_adapter.py and
      foundry/runtime/_langgraph_types.py
- [ ] YAML with invalid shape produces a Pydantic error with file +
      field + reason
- [ ] CostBudget enforcement: max_cost_usd: 0.01 against an over-budget
      project → CostBudgetExceeded raised pre-call; RunFailed
- [ ] TokenUsage.reasoning_tokens populated correctly
- [ ] Unit tests cover: provider lookup, capabilities introspection,
      config loading, env-var interpolation, secret-path handling,
      cost-budget check + record

WHEN COMPLETE:
1. Write handoff to docs/_phase_handoffs/phase_1.md including: provider
   credentials needed (env vars for tests), example project's location,
   any deviations.
2. Commit (one or multiple commits per logical chunk; conventional
   format; e.g., "feat(core): protocols + base classes",
   "feat(providers): anthropic + openai adapters", "feat(config):
   loader + schemas", etc.).
3. STOP at the exit gate. Do not start Phase 2.

DO NOT:
- Implement Tool dispatch, agent state visibility, multi-agent flow,
  evals, versioning, or anything beyond Phase 1 scope.
- Add LangSmith, Langfuse, or other backend integrations (later).
- Use real production credentials for tests; use env vars + mock as
  appropriate.
- Add Claude co-author lines.
```

### Review prompt for Phase 1

```
You are reviewing Phase 1 of agent-foundry. Read:
- docs/03-development-phases.md § Phase 1 (full)
- docs/10-core-framework.md, docs/11-provider-abstraction.md,
  docs/12-config-and-validation.md (the specs Phase 1 implements)
- docs/_phase_handoffs/phase_1.md
- Current repo state

VERIFY each exit-gate item from docs/03 § Phase 1. For each:
- Run the command (or describe what running it would do based on
  reading the code).
- Confirm the documented behaviour.
- Note specific evidence (file path + relevant code lines).

ADDITIONAL spec compliance checks:
- Are all the protocols from docs/10 § core types tour present and
  match documented signatures?
- Does the exception hierarchy match docs/10 § Exception hierarchy
  with to_dict() returning JSON-serialisable content?
- Does the ProviderAdapter base implement the documented hooks
  (_build_chat_model, _to_provider_messages, _from_provider_response,
  _stream_deltas, _classify_error)?
- Is the capability-required check active at compile time (not at
  runtime)?
- Does the YAML loader use SafeLoader (not FullLoader) per
  docs/12 § Implementation notes?

Report:
- Per exit gate: PASS / FAIL.
- Spec compliance: any deviations cited with doc reference + code path.
- Any concerns about test coverage or robustness.
- Verdict: PASS / PARTIAL / FAIL.

Do NOT implement; review only.
```

---

## Phase 2 — overview

**Source of truth**: `docs/03-development-phases.md` § Phase 2 — overview.

Phase 2 is delivered in three sub-phases on a strict dependency DAG: **2a → 2b → 2c**. Each sub-phase is a vertical slice: its own implementation session, AI review session, handoff note, manual smoke test, and demoable hero command. **Do not bundle them into one session** — the original Phase 2 (~13 days, ~35 exit-gate items) is too large to hold without drift, which is why this split exists.

Cumulative duration matches the monolithic estimate (10–14 days). The split exists for reviewability, not speed.

---

## Phase 2a — Tools + connections + catalog + state visibility

**Source of truth**: `docs/03-development-phases.md` § Phase 2a.

### Implementation prompt for Phase 2a

```
You are implementing Phase 2a of agent-foundry. Phase 1 is complete.
Phase 2 has been split into three sub-phases: 2a (this one) → 2b → 2c.

Read these docs FIRST (in this order):
- docs/03-development-phases.md § Phase 2a (deliverables + exit gate)
- docs/20-tool-system.md (FULL — load-bearing for this sub-phase)
- docs/21-agent-system.md (skim § Function nodes — that goes in 2c)
- docs/22-state-management.md (FULL — state visibility lands here)
- docs/23-connections-and-auth.md (FULL)
- docs/_phase_handoffs/phase_1.md (what's already built)

DELIVERABLES (per docs/03 § Phase 2a):
1. foundry.core.tool — Tool protocol + ToolRegistry.
2. foundry.core.connection — Connection, ConnectionPool, ConnectionAccessor,
   ConnectionFactory, ConnectionHealth, ConnectionDescriptor, AuthScheme enum.
3. foundry.core.state — Pydantic state primitive with reducer annotations
   (APPEND / MERGE / LWW / REPLACE_IF_SET).
4. foundry.config.schemas additions:
   - ToolSpec (incl. connections_required; DO NOT include cacheable,
     cache_ttl_s, cache_scope — those go in 2b).
   - AgentSpec extensions: tool allowlist + state-scope declarations.
     (DO NOT add semantic_cache, retrievers, memory — those are 2b/2c.)
   - StateSpec.
   - SystemSpec (tool + connection version pins).
   - ConnectionSpec + ConnectionBinding + refresh/pool policies.
5. foundry.config.refs — ArtifactRef parser + resolver for tool and
   connection kinds. (Retriever + agent_template kinds go in 2b.)
6. foundry.catalog — index loader, version discovery for tools and
   connections only. No promotion yet (Phase 5).
7. foundry.auth — 8 scheme helpers (api_key, basic_auth, oauth2_*,
   jwt_bearer, sigv4, mtls, custom) + token cache + redactor.
8. foundry.connections — ConnectionPool concrete impl + registry +
   health runner + ConnectionDescriptor builder.
9. foundry.orchestration.state_scope — compile-time per-node visibility
   enforcement (TypedDict projection per agent).
10. Per-tool + per-connection directory versioning on disk; versions.json
    metadata file shape per docs/22.
11. Compile-time wiring validation: ConnectionSlotNotBoundError,
    `accepts`-list mismatch error.
12. Catalog seeds:
    - catalog/tools/: 2–3 trivial shared tools.
    - catalog/connections/: postgres + pgvector + cohere_rerank
      (the latter two are USED by 2b but their connection shape lives
      here; their consumer types — retrievers + rerankers — come in 2b).
13. Update projects/hello/: hello_agent uses a catalog tool that declares
    a connection slot bound to a catalog connection.

EXIT GATE (full in docs/03 § Phase 2a):
- [ ] One-tool agent runs end-to-end: model → tool call (w/ pooled,
      authenticated connection) → tool result → model → final output
- [ ] Catalog tool ref AND catalog connection ref resolve through
      same code path
- [ ] Pin v1 → v2 in system.yaml (tool OR connection) → next run uses v2
- [ ] Tool output validated against schema → invalid raises structured
- [ ] Tool not in agent allowlist → ToolNotAllowedError
- [ ] Tool slot unbound → compile-time ConnectionSlotNotBoundError
- [ ] Tool slot's `accepts` mismatch → compile-time error
- [ ] Connection health: foundry connections health runs the eval
- [ ] Connection pool reuses connection across tool calls in same run
      (verified via pool metrics)
- [ ] Refresh on_auth_error: simulated 401 evicts + rebuilds
- [ ] Secret-literal scan catches credential in connection.config →
      ConfigLoadError
- [ ] State visibility: agent reads forbidden field → compile-time
      StateVisibilityError
- [ ] State reducers correct (APPEND / MERGE / LWW / REPLACE_IF_SET)
- [ ] Catalog index lists tools + connections with versions; missing
      version raises
- [ ] Per-tool + per-connection version directory layout matches spec
- [ ] hello_agent updated and runs end-to-end against catalog tool +
      connection

WHEN COMPLETE:
1. Handoff to docs/_phase_handoffs/phase_2a.md. Include:
   - Which catalog tools/connections were seeded.
   - Any deviations from spec.
   - Notes for 2b (e.g., interface shapes 2b will consume).
2. Multiple commits (suggested split: tool system / connections + auth /
   catalog + refs / state + visibility / hello_agent update).
3. STOP at exit gate. Do not start Phase 2b.

DO NOT:
- Implement cache / embedders / retrieval / rerankers (Phase 2b).
- Implement memory / FunctionNode / namespace-collision check (Phase 2c).
- Implement orchestration patterns beyond single-agent (Phase 3).
- Implement eval harness (Phase 4) or versioning (Phase 5).
- Add cacheable / cache_ttl_s / cache_scope to ToolSpec (Phase 2b).
- Add semantic_cache / retrievers / memory fields to AgentSpec
  (Phases 2b / 2c).
```

### Review prompt for Phase 2a

```
You are reviewing Phase 2a of agent-foundry.

Read:
- docs/03-development-phases.md § Phase 2a (full)
- docs/20-tool-system.md, docs/22-state-management.md,
  docs/23-connections-and-auth.md (the specs Phase 2a implements)
- docs/_phase_handoffs/phase_2a.md
- Repo state

VERIFY each exit-gate item from docs/03 § Phase 2a. For each:
- Confirm behaviour matches the spec.
- Note specific evidence (file paths + relevant code).

CRITICAL spec-compliance checks beyond exit gate:
- Tool: 5-file shape enforced at registry load? (docs/20 § The 5-file
  shape)
- Tool dispatch: 12-step pipeline matches docs/20 § Dispatch?
- Connection sandbox: ConnectionDescriptor.redacted_config truly
  redacts (no secrets in span attributes via lint or test)?
- State visibility: TypedDict projection generated per agent (forbidden
  fields literally absent from agent's view, not just runtime-rejected)?
- Catalog index: version discovery handles missing LATEST file
  gracefully per docs/22?
- AgentSpec scope: confirm NO semantic_cache / retrievers / memory
  fields were added (those are 2b/2c — leakage is a fail).
- ToolSpec scope: confirm NO cacheable / cache_ttl_s / cache_scope
  fields (those are 2b).

Report:
- Per exit-gate item: PASS / FAIL with evidence.
- Spec compliance per critical checks.
- Out-of-scope leakage (2a containing 2b/2c work) — FAIL if any.
- Verdict: PASS / PARTIAL / FAIL.
- If PARTIAL: ordered list of specific gaps for the next session.

Do NOT implement; review only.
```

---

## Phase 2b — Cache + embedders + retrieval

**Source of truth**: `docs/03-development-phases.md` § Phase 2b.

### Implementation prompt for Phase 2b

```
You are implementing Phase 2b of agent-foundry. Phases 0, 1, 2a complete.

Read these docs FIRST:
- docs/03-development-phases.md § Phase 2b (deliverables + exit gate)
- docs/24-caching-and-optimisation.md (FULL)
- docs/25-retrieval-and-rag.md (FULL)
- docs/_phase_handoffs/phase_2a.md (what's already built —
  especially the connection types you'll consume for vector stores
  and rerankers)

DELIVERABLES (per docs/03 § Phase 2b):
1. foundry.core.embedder — Embedder protocol, Embedding,
   EmbedderCapabilities.
2. foundry.core.retrieval — Retriever, Reranker protocols,
   RetrievedDocument.
3. foundry.core.cache — SemanticCache, ResultCache, CacheAccessor
   protocols, key types (incl. SemanticCacheKey per docs/24 § Key
   construction).
4. foundry.config.schemas additions:
   - ToolSpec: cacheable, cache_ttl_s, cache_scope.
   - AgentSpec: semantic_cache (SemanticCacheConfig | None),
     retrievers (list[RetrieverBinding]).
   - New schemas: RetrieverBinding, RerankerBinding,
     SemanticCacheConfig, EmbedderBinding.
5. foundry.config.refs — extend resolver for retriever + agent_template
   kinds.
6. foundry.catalog — extend version discovery for retrievers.
7. foundry.providers.embedders — Voyage + OpenAI + Cohere + Bedrock
   embedder adapters.
8. foundry.cache — concrete SemanticCache + ResultCache:
   in_process (SQLite/FAISS), redis (Redis Stack), pgvector
   (Postgres pgvector — uses the pgvector connection from 2a).
9. foundry.retrieval — DenseRetriever, SparseRetriever, HybridRetriever
   (with RRF) + reranker adapters (Cohere, Voyage, Jina, local
   cross-encoder stub).
10. Compile-time wiring validation for retriever bindings and cache
    backends (dimension match against embedder).
11. Catalog seeds: catalog/retrievers/pgvector_dense +
    catalog/retrievers/hybrid_rrf templates.
12. Second example project (suggest projects/rag_hello/) demonstrating
    semantic cache + hybrid retriever end-to-end.

EXIT GATE (per docs/03 § Phase 2b):
- [ ] Embedder round-trip: Voyage voyage-3 + OpenAI text-embedding-3-small
      both resolve and produce embeddings of advertised dims
- [ ] Semantic cache hit: in_process backend hits on re-run;
      cache.semantic.hit event with similarity ≥ threshold;
      saved_cost_usd populated
- [ ] Semantic cache invalidation on prompt-version bump; invalidate
      event emitted
- [ ] Tool-result cache: cacheable: true + cache_ttl_s: 60 → second
      call returns cached output; cache.tool.hit emitted
- [ ] Tool-cache validator: cacheable: true without cache_ttl_s →
      ConfigValidationError at load
- [ ] Cache failure fails open: backend raises → run completes via
      LLM + warning event
- [ ] Hybrid retriever: dense + sparse in parallel + RRF merge;
      retrieval event emitted; one-branch-fail-other-branch-return
      test passes
- [ ] Reranker: cohere_rerank reorders docs; rerank event with
      cost_estimate emitted
- [ ] Dimension mismatch compile check: embedder dim ≠ vector store
      dim → EmbedderConfigError at load
- [ ] Second example project runs end-to-end with semantic cache +
      hybrid retriever

WHEN COMPLETE:
1. Handoff to docs/_phase_handoffs/phase_2b.md.
2. Commits per logical chunk (suggested split: embedder protocols +
   adapters / cache protocols + impls / retrieval protocols + impls /
   reranker adapters / second example project).
3. STOP. Do not start Phase 2c.

DO NOT:
- Implement memory layers / FunctionNode / Node protocol (Phase 2c).
- Implement orchestration patterns (Phase 3).
- Add memory field to AgentSpec (Phase 2c).
```

### Review prompt for Phase 2b

```
You are reviewing Phase 2b of agent-foundry.

Read:
- docs/03-development-phases.md § Phase 2b
- docs/24-caching-and-optimisation.md, docs/25-retrieval-and-rag.md
- docs/_phase_handoffs/phase_2b.md
- Repo state

VERIFY each exit-gate item from docs/03 § Phase 2b.

CRITICAL spec-compliance checks beyond exit gate:
- Cache: SemanticCacheKey shape includes structural hash + embedding
  per docs/24 § Key construction?
- Cache fail-open: backend down → warning + LLM call (not exception)?
- Hybrid RRF: implementation matches the formula in docs/25 §
  Hybrid retrieval (1 / (k + rank), summed)?
- Reranker: cost_estimate populated even when adapter doesn't return
  cost (defensive default)?
- Dimension check happens at load, NOT first call?
- AgentSpec scope: confirm memory field NOT added (Phase 2c).

Report:
- Per exit-gate item: PASS / FAIL.
- Spec compliance per critical checks.
- Out-of-scope leakage — FAIL if Phase 2c content snuck in.
- Verdict: PASS / PARTIAL / FAIL.

Do NOT implement; review only.
```

---

## Phase 2c — Memory + FunctionNode

**Source of truth**: `docs/03-development-phases.md` § Phase 2c.

### Implementation prompt for Phase 2c

```
You are implementing Phase 2c of agent-foundry. Phases 0, 1, 2a, 2b
complete.

Read these docs FIRST:
- docs/03-development-phases.md § Phase 2c (deliverables + exit gate)
- docs/21-agent-system.md § Function nodes (FULL)
- docs/26-memory-and-context.md (FULL)
- docs/_phase_handoffs/phase_2b.md (the retriever interface you'll
  consume for episodic memory)

DELIVERABLES (per docs/03 § Phase 2c):
1. foundry.core.node + foundry.core.function_node:
   - Node protocol (parent of Agent + FunctionNode).
   - FunctionNode protocol, BaseFunctionNode, NodeResult.
   - Deterministic-Python nodes with same state-visibility /
     observability / retry plumbing as agents but no LLM.
2. foundry.core.memory — Memory, MemoryLayer protocols;
   MemoryEnvelope, MemoryContribution, MemoryWrite, MemoryContext.
3. foundry.config.schemas additions:
   - AgentSpec: memory (MemoryConfig | None).
   - FunctionNodeSpec.
4. foundry.memory:
   - DefaultMemory coordinator.
   - WorkingMemoryLayer (windowed state field).
   - EpisodicMemoryLayer (wraps a Retriever from 2b).
   - SemanticMemoryLayer (state field + periodic consolidator).
   - prompt_assembly: envelope → prompt injection (system_prefix /
     system_suffix / user_prefix placements per docs/26).
5. Remaining compile-time wiring validation:
   - Node namespace collision (agent + function same name) →
     CompileError.
   - Mixed-flow validation: graph flow's from/to refs resolve to
     either agents or functions interchangeably.
6. Third example project demonstrating memory layers + a FunctionNode
   in a sequential flow.

EXIT GATE (per docs/03 § Phase 2c):
- [ ] Memory: working layer windowing (max_messages: 5 → last 5 turns
      in prompt on 10-turn run)
- [ ] Memory: episodic against seeded retriever; memory.read event
      lists episodic in layers_read
- [ ] Memory: semantic with periodic consolidation; memory.consolidate
      event with token counts
- [ ] Memory: degrade-gracefully (default) — failed retriever in
      episodic → empty contribution + warning + run completes
- [ ] Memory: fail-strict mode — same failure → MemoryLayerError +
      run aborts
- [ ] Memory: envelope token cap — max_envelope_tokens triggers
      truncation of last-listed layer first; truncated: true in event
- [ ] Memory: layer-name uniqueness → ConfigValidationError at load
- [ ] FunctionNode end-to-end: sequential flow [normalize_input,
      hello_agent, format_output] runs; final state reflects full
      pipeline
- [ ] FunctionNode state visibility: read: [a, b], write: [c] returning
      {a, c} → only c written; a dropped + warning event
- [ ] FunctionNode observability: started/completed events with
      node_name, node_version, fields_written, bytes_delta, latency_ms
- [ ] Node namespace collision (agent + function same name) →
      CompileError
- [ ] Mixed flow: from/to refs resolve across agents and functions;
      missing reference → CompileError

WHEN COMPLETE:
1. Handoff to docs/_phase_handoffs/phase_2c.md.
2. Commits per logical chunk (suggested split: Node + FunctionNode
   protocols / memory protocols / memory coordinator + layers /
   prompt assembly / compile-time validation / third example project).
3. STOP. Phase 2 complete. Do not start Phase 3.

DO NOT:
- Implement orchestration patterns beyond what 2a/2b/single already
  provide (Phase 3 fully covers sequential/parallel/supervisor/graph).
- Implement eval (Phase 4), versioning (Phase 5), meta-agent (Phase 6),
  multi-agent + HITL (Phase 7), API (Phase 8), observability hardening
  (Phase 9).
```

### Review prompt for Phase 2c

```
You are reviewing Phase 2c of agent-foundry. This closes out Phase 2.

Read:
- docs/03-development-phases.md § Phase 2c
- docs/21-agent-system.md § Function nodes,
  docs/26-memory-and-context.md
- docs/_phase_handoffs/phase_2c.md (plus 2a + 2b for full Phase 2
  context)
- Repo state

VERIFY each exit-gate item from docs/03 § Phase 2c.

CRITICAL spec-compliance checks beyond exit gate:
- FunctionNode: does NOT have model_binding / tools / iteration_limit
  fields per docs/21 § Function nodes?
- Memory layer: state-field visibility check at compile (memory layers
  declaring a state field they don't have read access to → CompileError)?
- Memory envelope: layer order in prompt-assembly follows declaration
  order per docs/26 § Envelope assembly?
- Consolidator: uses agent's model_binding, NOT a separate one (cost
  attribution stays clean)?
- Mixed-flow: a single graph flow can reference both agents and
  functions in from/to without quoting / kind prefixes (interchangeable)?

ADDITIONAL Phase 2 cumulative checks:
- All 2a + 2b + 2c exit-gate items still PASS (no regressions).
- AgentSpec final shape contains: tool allowlist, state scope,
  semantic_cache, retrievers, memory — ALL of them.
- ToolSpec final shape contains: connections_required, cacheable,
  cache_ttl_s, cache_scope.

Report:
- Per exit-gate item (Phase 2c): PASS / FAIL.
- Regression check across 2a + 2b: NO REGRESSIONS / REGRESSIONS FOUND.
- Verdict: PASS / PARTIAL / FAIL.
- If PASS: Phase 2 is complete; Phase 3 unblocked.

Do NOT implement; review only.
```

---

## Phase 3 — Single-agent orchestration on LangGraph

**Implementation prompt** + **review prompt** follow the same pattern. For brevity, the remaining phases are sketched with key prompt elements; expand as you reach each phase.

### Implementation prompt skeleton (Phase 3)

```
You are implementing Phase 3 per docs/03-development-phases.md § Phase 3.
Phases 0–2 complete.

READ:
- docs/03-development-phases.md § Phase 3
- docs/30-orchestration-patterns.md (focus on `single` pattern; skim
  others for framing)
- docs/31-multi-agent-systems.md § Compile pipeline
- docs/_phase_handoffs/phase_2.md

DELIVERABLES (per docs/03 § Phase 3):
- foundry.orchestration.compiler — SystemSpec → CompiledSystem.
  Single-agent pattern first. Parallel/sequential/supervisor/graph
  stubbed (Phase 7 fills out).
- foundry.runtime.langgraph_adapter — full single-agent flow:
  StateGraph construction, async node, checkpointer wiring, streaming.
- foundry.runtime.checkpointers — in-memory (for tests) + SQLite (dev).
- CLI: foundry run supports --stream + --checkpoint sqlite|memory|none.

EXIT GATE: per docs/03 § Phase 3.

WHEN COMPLETE: handoff note + commits + STOP.

DO NOT: implement patterns beyond `single` (Phase 7); implement HITL
(Phase 7); implement eval (Phase 4); add API layer (Phase 8).
```

### Review prompt skeleton (Phase 3)

```
You are reviewing Phase 3.

READ: docs/03 § Phase 3 + docs/30 + docs/31 + handoff.

VERIFY exit-gate items. CRITICAL CHECKS:
- LangGraph imports confined to runtime/langgraph_adapter.py (lint check)
- Checkpoint kill+restart works (run survives process death)
- foundry.run span attributes match docs/01 § Observability event spec

Report verdict + gaps.
```

---

## Phase 4 — Eval harness + per-artifact + comparison

### Implementation prompt skeleton

```
You are implementing Phase 4. Phases 0–3 complete.

READ:
- docs/03 § Phase 4
- docs/40-eval-harness.md (full)
- docs/41-eval-driven-iteration.md (focus on FailureClustering;
  iteration loop is Phase 6)
- docs/_phase_handoffs/phase_3.md

DELIVERABLES (per docs/03 § Phase 4):
- foundry.eval.schemas — EvalSpec, EvalCase, ScorerConfig, EvalRunResult,
  EvalComparison.
- foundry.eval.harness — async runner for tool / agent / project scopes.
- foundry.eval.compare — cross-version + cross-pin-set.
- foundry.eval.scorers — exact / numeric / llm_judge / rubric +
  user-pluggable entry-point discovery.
- foundry.eval.reporter — CLI table + JSON.
- CLI: foundry eval / foundry eval tool / agent / compare.

EXIT GATE: per docs/03 § Phase 4.

WHEN COMPLETE: handoff + commits + STOP.

DO NOT: implement iteration loop (Phase 6); implement versioning
(Phase 5); implement meta-agent (Phase 6).
```

### Review prompt skeleton

```
You are reviewing Phase 4.

READ: docs/03 § Phase 4 + docs/40 + handoff.

VERIFY exit-gate. CRITICAL CHECKS:
- Three scopes share one harness; differ only in target shape
- LLM-judge scorer uses provider abstraction (not hardcoded vendor)
- Determinism: fixed seed → reproducible scores within tolerance
- Exit codes: 0 / 1 / 2 distinguishable per docs/40 § CI integration

Report verdict + gaps.
```

---

## Phase 5 — Versioning + git backbone + per-artifact rollback + catalog promote

### Implementation prompt skeleton

```
You are implementing Phase 5. Phases 0–4 complete.

READ:
- docs/03 § Phase 5
- docs/50-versioning-model.md
- docs/51-git-backbone.md
- docs/52-rollback-and-audit.md
- docs/_phase_handoffs/phase_4.md

DELIVERABLES (per docs/03 § Phase 5):
- foundry.versioning.git_backend — subprocess git wrapper.
- foundry.versioning.artifacts — per-artifact version I/O.
- foundry.versioning.pins — read/write helpers; transactional.
- foundry.versioning.rollback — three modes (per-tool / per-prompt /
  per-project) with mandatory pre-flight checks.
- foundry.catalog.promote — human-gated; eval floor; tool + connection
  promotion.
- foundry.versioning.audit — append-only .foundry/audit.jsonl per
  project; AuditEntry Pydantic.
- foundry.versioning.refs — ArtifactRef resolution against on-disk
  structure.
- CLI: foundry rollback / versions / diff / catalog promote.

EXIT GATE: per docs/03 § Phase 5.

WHEN COMPLETE: handoff + commits + STOP.

DO NOT: implement meta-agent's git tools (Phase 6 — they wrap these);
implement deployment rollback (Phase 8).
```

### Review prompt skeleton

```
You are reviewing Phase 5.

READ: docs/03 § Phase 5 + docs/50 + docs/51 + docs/52 + handoff.

VERIFY exit-gate. CRITICAL CHECKS:
- Per-tool rollback edits ONLY system.yaml (single-file commit)
- Per-prompt rollback edits ONLY agent.yaml
- Per-project rollback is atomic (all files or none)
- Pre-flight refuses on dirty working tree (unless --force)
- Audit entries match AuditEntry shape; append-only enforced
- Catalog promotion refuses below floor; warns on schema-breaking

Report verdict + gaps.
```

---

## Phase 6 — Meta-agent

### Implementation prompt skeleton

```
You are implementing Phase 6. Phases 0–5 complete.

READ:
- docs/03 § Phase 6
- docs/60-meta-agent.md (full)
- docs/61-meta-tools.md (full)
- docs/62-configurator-sessions.md (full — sessions structure)
- docs/41-eval-driven-iteration.md (the iteration loop logic)
- docs/_phase_handoffs/phase_5.md

DELIVERABLES (per docs/03 § Phase 6):
- foundry.configurator.meta_agent — MetaAgent class (a foundry.Agent
  subclass).
- foundry.configurator.tools — full meta-tool catalogue (file /
  discovery / scaffold / eval / versioning per docs/61).
- foundry.configurator.session — iteration loop: bootstrap +
  iterate + termination per docs/41.
- Meta-agent prompt at foundry/configurator/prompts/v1.md per docs/60
  § Prompt structure.
- CLI: foundry project new / foundry forge.

EXIT GATE: per docs/03 § Phase 6.

WHEN COMPLETE: handoff + commits + STOP.

DO NOT: implement multi-agent orchestration patterns (Phase 7);
implement HITL (Phase 7); implement API layer (Phase 8).
```

### Review prompt skeleton

```
You are reviewing Phase 6.

READ: docs/03 § Phase 6 + docs/60 + docs/61 + docs/62 + handoff.

VERIFY exit-gate. CRITICAL CHECKS:
- Meta-agent's write_file refused outside scoped project (sandbox)
- build_tool refuses dangerous: true
- build_agent refuses provider_overrides
- Forbidden git ops refused at meta-tool layer (BEFORE subprocess)
- Forge produces correct trajectory artifact format (docs/62
  § Trajectory artifact)
- Each iteration is a structured commit per docs/51 commit conventions
- Cost budget enforced: forge halts on cost cap
- Plateau detection: no_improvement_after triggers termination

Report verdict + gaps.
```

---

## Phase 7 — Multi-agent orchestration + HITL

### Implementation prompt skeleton

```
You are implementing Phase 7. Phases 0–6 complete.

READ:
- docs/03 § Phase 7
- docs/30-orchestration-patterns.md (FULL — all five patterns now)
- docs/31-multi-agent-systems.md (FULL)
- docs/32-human-in-the-loop.md (FULL)
- docs/_phase_handoffs/phase_6.md

DELIVERABLES (per docs/03 § Phase 7):
- foundry.orchestration.patterns — supervisor / sequential / parallel /
  graph compilers (single is from Phase 3). Each as a reusable helper.
- Compiler full support for nested flows.
- foundry.orchestration.hitl — ApprovalRequired + interrupt/resume
  wiring to LangGraph interrupt().
- Compile-time generation of typed handoff tools for supervisor pattern.
- Predicate sandbox AST validator for graph patterns.
- State visibility enforcement across multi-agent graphs (subgraph-per-
  agent with scoped schemas).
- CLI: foundry resume <run_id> --approve / --reject --reason "...".

EXIT GATE: per docs/03 § Phase 7.

WHEN COMPLETE: handoff + commits + STOP.

DO NOT: implement API layer (Phase 8); implement observability beyond
the existing event emission.
```

### Review prompt skeleton

```
You are reviewing Phase 7.

READ: docs/03 § Phase 7 + docs/30 + docs/31 + docs/32 + handoff.

VERIFY exit-gate. CRITICAL CHECKS:
- All five patterns compile + run end-to-end on minimal fixtures
- Workers cannot read fields outside their visibility (assertion
  in test)
- Parallel fan-out + fan-in: reducers correct on concurrent writes
- HITL: approval pending → process restart → resume completes
- max_hops + max_iterations enforced cleanly
- Predicate sandbox: forbidden constructs (function calls, comprehensions)
  raise CompileError

Report verdict + gaps.
```

---

## Phase 8 — API + streaming + scaling + async polish

### Implementation prompt skeleton

```
You are implementing Phase 8. Phases 0–7 complete.

READ:
- docs/03 § Phase 8
- docs/70-api-layer.md (FULL)
- docs/71-async-runtime.md (FULL)
- docs/85-batch-and-throughput.md (FULL — includes sustained-load gate)
- docs/_phase_handoffs/phase_7.md

DELIVERABLES (per docs/03 § Phase 8):
- foundry.api.app — FastAPI factory; per-project endpoint generation.
- foundry.api.streaming — SSE encoder with Last-Event-ID resume;
  WebSocket handler for InboundMessage union.
- foundry.api.routes — POST /run, /stream, /batch, WS /ws, GET /runs/
  {id}, /runs/{id}/events, POST /runs/{id}/resume, GET /health,
  /config.
- foundry.api.batch — batch executor.
- foundry.api.worker — worker identity tagging.
- Multi-worker prod shape: uvicorn --workers N; Postgres checkpointer
  + Redis rate limiter as configurable.
- foundry.providers.rate_limit — InProcessTokenBucket +
  RedisTokenBucket.
- Cancellation + timeout polish (graceful shutdown per docs/71
  § Graceful shutdown).
- CLI: foundry serve --workers N.

EXIT GATE: per docs/03 § Phase 8 (incl. sustained-load test).

WHEN COMPLETE: handoff + commits + STOP.

DO NOT: implement observability backends (Phase 9); implement security
hardening beyond what's already in place (Phase 9).
```

### Review prompt skeleton

```
You are reviewing Phase 8.

READ: docs/03 § Phase 8 + docs/70 + docs/71 + docs/85 + handoff.

VERIFY exit-gate. CRITICAL CHECKS:
- OpenAPI schema at /openapi.json validates + matches SystemSpec types
- SSE Last-Event-ID resume: kill client, reconnect, replay from N+1
- WebSocket: InboundMessage handling for all 5 kinds
- Batch + cost budget: per-batch cap enforced across multi-worker
- Graceful shutdown: SIGTERM → drain → cancel → close pools → exit
- Sustained-load: 100 runs/sec for 5 min; zero dropped events;
  p95 latency within 2× of baseline

Report verdict + gaps.
```

---

## Phase 9 — Observability + dev UX + security + deploy

### Implementation prompt skeleton

```
You are implementing Phase 9 (the final phase). Phases 0–8 complete.

READ:
- docs/03 § Phase 9
- docs/80-observability.md (FULL)
- docs/81-storage-and-artifacts.md (FULL)
- docs/82-dev-ux.md (FULL — includes foundry.testing fixtures)
- docs/83-security-guardrails.md (FULL)
- docs/84-deployment.md (FULL)
- docs/86-multi-tenancy-and-ip.md (FULL)
- docs/_phase_handoffs/phase_8.md

DELIVERABLES (per docs/03 § Phase 9):
- foundry.observability.tracing — OTel setup; spans for foundry.run /
  node / llm / tool / handoff / state_transition / eval / function_node
  / connection / embed / cache / retrieval / rerank / memory.
- foundry.observability.metrics — full metric catalogue.
- foundry.observability.store — local SQLite event-mirror.
- foundry.cli.obs — query commands.
- Optional LangSmith / Langfuse exporters.
- foundry.storage — Filesystem / S3 / Azure Blob / GCS backends;
  retention + archival; pinned retention.
- foundry.cli.tui — review TUI per docs/82 (commits + forges +
  approvals + connections tabs).
- foundry.security — sandbox module, prompt-injection guardrails,
  validators.
- foundry.testing — fixtures (RunContextFixture, MockConnection,
  MockProvider, MockEmbedder, MockRetriever, MockReranker, etc.) +
  state helpers.
- foundry.cli.test — pytest wrapper.
- foundry.cli.deploy — deploy command + platform helpers (kubectl /
  ecs / cloud-run / fly / nomad / noop).
- foundry.cli.doctor — full doctor checks per docs/82.
- Sample Dockerfile + per-platform manifest examples in docs/examples/
  or deploy/ template.

EXIT GATE: per docs/03 § Phase 9 (the most extensive — read it carefully).

WHEN COMPLETE:
1. Final handoff to docs/_phase_handoffs/phase_9.md.
2. Comprehensive commit set.
3. Update docs/README.md with v1 status.
4. Tag a v1.0.0 release.
5. STOP. v1 is complete.

DO NOT: implement v1.1+ features per personal_docs/study_plan.md
references to v1.1 backlog (mid-iteration HITL pause, drift daemon,
forge web UI, etc.).
```

### Review prompt skeleton

```
You are reviewing Phase 9 (final).

READ: docs/03 § Phase 9 + docs/80 through docs/86 + handoff.

VERIFY exit-gate. CRITICAL CHECKS:
- Every RunEvent type from docs/10 § Streaming events emitted
  somewhere
- foundry obs queries return correct data matching OTel stream
- Storage backends: round-trip put/get/list/delete works for each
- Retention: GC respects pinned items; force overrides logged
- Review TUI: launches; navigation works; rollback action commits
- Security: contract test for credential leak (known fake key in
  fixtures); zero hits in any output
- Sandbox: 50-case fuzz of malicious paths all refused
- Sample manifests: each platform's manifest is syntactically valid
- foundry test: runs pytest with foundry fixtures auto-loaded
- foundry doctor: all checks operational

Report verdict + gaps.

If PASS: confirm v1 is complete + suggest next steps (v1.1+ planning,
deployment to a real environment, etc.).
```

---

## Cross-cutting prompts

### Mid-phase recovery prompt

If a phase implementation session goes off-track or runs out of context:

```
You are continuing implementation of Phase <N> of agent-foundry. The
prior implementation session was interrupted.

READ:
- docs/03-development-phases.md § Phase <N>
- The relevant tier docs for Phase <N> (specified in the original
  prompt above)
- Any partial handoff at docs/_phase_handoffs/phase_<N>_partial.md
  if it exists
- git log --oneline -20 to see what's been committed so far

ASSESS the current state:
- Which deliverables are complete (committed)?
- Which are partial (uncommitted)?
- Which are missing entirely?

CONTINUE from where the prior session left off. Same exit gate; same
DO-NOT list as the original Phase <N> prompt.

WHEN COMPLETE: handoff to docs/_phase_handoffs/phase_<N>.md (overwrite
any partial). Commit + STOP.
```

### Cross-phase regression check (end of any phase)

After any phase completes, optionally run:

```
You are running a regression check after Phase <N> completion.

READ:
- All exit gates from docs/03 for Phases 0 through <N>
- The handoff notes for Phases 0 through <N>

VERIFY:
- All earlier-phase exit gates still pass (Phase <N> didn't break
  prior work)
- No regressions in test suite
- No new lint violations
- No new import-boundary violations
- Doc references in code remain accurate

Report:
- For each prior phase: REGRESSION-CLEAN / REGRESSION-FOUND
- If regression found: specific gate that broke + likely cause

Do NOT fix; flag for the next implementation session.
```

---

## Tracking

Per-phase status (mark on completion):

- [ ] Phase 0 — Decisions & skeleton
- [ ] Phase 1 — Core framework + provider + config
- [ ] Phase 2a — Tools + connections + catalog + state visibility
- [ ] Phase 2b — Cache + embedders + retrieval
- [ ] Phase 2c — Memory + FunctionNode
- [ ] Phase 3 — Single-agent orchestration on LangGraph
- [ ] Phase 4 — Eval harness
- [ ] Phase 5 — Versioning + git + rollback + catalog promote
- [ ] Phase 6 — Meta-agent
- [ ] Phase 7 — Multi-agent orchestration + HITL
- [ ] Phase 8 — API + streaming + scaling
- [ ] Phase 9 — Observability + dev UX + security + deploy

## Lessons-learned section (operator fills in)

After each phase, add a brief note here capturing what worked / didn't:

### Phase 0 — Lessons

(fill in after completion)

### Phase 1 — Lessons

(fill in after completion)

(...etc...)

---

## Notes on the fresh-session pattern

Why fresh sessions per phase:

1. **Context bloat is real**. A single session attempting all 10 phases would accumulate ~100k+ tokens of conversation history; reasoning quality drifts.
2. **Drift between intent and execution**. As context grows, the session loses the original directive; details slip.
3. **Auditability**. Each phase's session has a focused conversation; reviewing what happened post-hoc is tractable.
4. **Independent review**. The review session has no commitment bias from the implementation; cleaner judgement.
5. **Recovery from drift**. If a session goes wrong, you discard it + start fresh with the same prompt; nothing's lost (commits are the source of truth).

Patterns to avoid:

- Don't paste mid-phase corrections into the implementation session — it pollutes context. Either let it complete + review fixes the issues, OR start a fresh session with a recovery prompt.
- Don't have the implementation session also do its own review — bias.
- Don't skip the handoff note — it's the only context the next session has.
- Don't implement multiple phases in one session even if "they're small" — drift compounds.

## When you're done with v1

When Phase 9 ships + the review session passes, you have agent-foundry v1. From there:

1. Tag the release: `git tag v1.0.0 && git push --tags`.
2. Pick a real project to forge against (e.g., a small triage agent for a real workflow).
3. Capture lessons; refine the meta-agent prompt over time (its prompt is framework-versioned per `60`).
4. Plan v1.1 work from the v1.1 backlog (in memory: `project_v1_1_backlog.md`).
5. Decide whether to open-source the framework or keep private.

Refer back to this implementation plan + `personal_docs/study_plan.md` whenever you need to ground a decision in the original design.
