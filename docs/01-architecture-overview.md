# 01 — Architecture Overview

## Purpose

This doc shows the whole of `agent-foundry` on one page: the layers, the primitives, the lifecycle, the dataflow, the module layout, and the runtime-vs-configurator boundary. Every tier below this expands one layer or component in depth. Read this before any tier doc.

## The two systems

```
┌──────────────────────────────────┐        ┌──────────────────────────────────┐
│  CONFIGURATOR (dev-time)         │        │  RUNTIME (run-time)              │
│                                  │        │                                  │
│  - Meta-agent (an agent itself)  │        │  - Compiled agent systems        │
│  - Reads/writes config files     │ writes │  - LangGraph execution           │
│  - Runs eval harness             │───────▶│  - Provider-abstracted LLM calls │
│  - Commits to git (versioning)   │ config │  - Tool execution                │
│  - CLI: `foundry forge ...`      │ files  │  - Checkpointing, interrupts     │
│  - Never touches live services   │        │  - Trace + run artifact output   │
└──────────────────────────────────┘        └──────────────────────────────────┘
         │                                           │
         │ both use                                  │
         ▼                                           ▼
┌──────────────────────────────────────────────────────────────────────────────┐
│                           SHARED FOUNDRY CORE                                │
│  Agent/Tool/Session primitives · Pydantic schemas · Provider abstraction     │
│  Config loader · State schema · Observability · Storage                      │
└──────────────────────────────────────────────────────────────────────────────┘
```

The configurator only writes files. The runtime only reads files and executes. They share the core foundry primitives but never call each other directly. This separation — inherited from the seed idea in `personal_docs/meta-agent-configurator.jsx` — is load-bearing: the meta-agent cannot corrupt a running agent because it has no handle on running agents.

## Layer stack

From bottom (concrete) to top (user-facing):

```
┌──────────────────────────────────────────────────────────────────────────────┐
│  TIER 8 — OPS                                                                │
│  observability · storage · dev UX (CLI, REPL, review UI) · security · deploy │
├──────────────────────────────────────────────────────────────────────────────┤
│  TIER 7 — API & RUNTIME                                                      │
│  FastAPI server · async runtime (event loop, cancellation, resumption)       │
├──────────────────────────────────────────────────────────────────────────────┤
│  TIER 6 — META-AGENT (configurator, dev-time only)                           │
│  meta-agent definition · meta-tools (read/write/eval/git) · sessions         │
├──────────────────────────────────────────────────────────────────────────────┤
│  TIER 5 — VERSIONING & ROLLBACK                                              │
│  versioning model · git backbone · rollback & audit                          │
├──────────────────────────────────────────────────────────────────────────────┤
│  TIER 4 — EVAL                                                               │
│  eval harness · eval-driven iteration                                        │
├──────────────────────────────────────────────────────────────────────────────┤
│  TIER 3 — ORCHESTRATION                                                      │
│  patterns · multi-agent systems · HITL                                       │
├──────────────────────────────────────────────────────────────────────────────┤
│  TIER 2 — AGENTS & TOOLS                                                     │
│  tool system · agent system · state management                               │
├──────────────────────────────────────────────────────────────────────────────┤
│  TIER 1 — CORE FRAMEWORK                                                     │
│  Agent/Tool/Session primitives · provider abstraction · config & validation  │
├──────────────────────────────────────────────────────────────────────────────┤
│  RUNTIME ADAPTER                                                             │
│  langgraph_adapter.py — the only place LangGraph is imported                 │
├──────────────────────────────────────────────────────────────────────────────┤
│  LANGGRAPH + LANGCHAIN (third-party)                                         │
│  graph execution · checkpointing · interrupts · init_chat_model              │
└──────────────────────────────────────────────────────────────────────────────┘
```

Dependencies flow downward only. Tier 1 does not import from Tier 2; Tier 4 does not import from Tier 6. This is a hard rule enforced by an import-boundary lint (see `10-core-framework.md`).

## Primitives

Eight Pydantic models form the shared vocabulary of the entire foundry. Every other concept is composed from these.

| Primitive | Defined in | Owned by | One-line description |
|---|---|---|---|
| `ToolSpec` | `20-tool-system.md` | Tier 2 | A tool's identity, schema, handler reference, connection slots, versioning metadata. One per tool version. |
| `AgentSpec` | `21-agent-system.md` | Tier 2 | A single agent's identity: model binding, prompt ref, tools (with version pins), output schema, state contract. |
| `StateSpec` | `22-state-management.md` | Tier 2 | A system's shared state schema; per-node visibility rules. |
| `ConnectionSpec` | `23-connections-and-auth.md` | Tier 2 | A pooled, authenticated handle to an external enterprise system (Snowflake, Slack, S3, Salesforce, …). Standalone versioned artifact; tools declare slots that bind to connections. |
| `ConnectionBinding` | `23-connections-and-auth.md` | Tier 2 | Project-level pin of a connection ref, version, config, and credentials_ref. Lives inside `SystemSpec.connections`. |
| `Embedder` / `Embedding` | `11-provider-abstraction.md` + `24-caching-and-optimisation.md` | Tier 1/2 | Protocol + typed vector result for text embeddings. Separate vendor family from generation providers (Voyage, OpenAI, Cohere, Bedrock). |
| `SemanticCache` / `SemanticCacheConfig` | `24-caching-and-optimisation.md` | Tier 2 | Optional similarity-based cache for LLM calls; opt-in per agent; correctness-sensitive so off by default. |
| `ResultCache` + `ToolSpec.cacheable` | `24-caching-and-optimisation.md` + `20-tool-system.md` | Tier 2 | Exact-match cache for idempotent tool outputs; opt-in per tool. |
| `Retriever` / `Reranker` / `RetrievedDocument` | `25-retrieval-and-rag.md` | Tier 2 | RAG primitives: dense/sparse/hybrid retrieval + cross-encoder reranking. |
| `RetrieverBinding` / `RerankerBinding` | `25-retrieval-and-rag.md` | Tier 2 | Agent-level pin of retrievers (and optional reranker) with connection bindings. |
| `Memory` / `MemoryLayer` | `26-memory-and-context.md` | Tier 2 | Multi-layer memory coordinator + per-layer protocol. Composes existing primitives (state, retriever, hooks). |
| `MemoryConfig` + `MemoryLayerConfig` | `26-memory-and-context.md` | Tier 2 | Agent-level config: which layers, their settings, prompt-injection rules. Off by default. |
| `SystemSpec` | `31-multi-agent-systems.md` | Tier 3 | A full multi-agent system: agents + flow + state + guardrails + tool version pins + connection bindings. The manifest. |
| `EvalSpec` | `40-eval-harness.md` | Tier 4 | An eval set: cases, scorers, threshold, metadata. Attached to a tool version, an agent, a project, or a connection health check. |
| `EvalComparison` | `40-eval-harness.md` | Tier 4 | Result of running the same eval against multiple artifact versions or pin-sets. |
| `RunArtifact` | `80-observability.md` | Tier 8 | The frozen record of a single execution: inputs, outputs, trace, metrics, id. |
| `ModelBinding` | `11-provider-abstraction.md` | Tier 1 | Provider + model + settings + capabilities. |
| `CatalogEntry` | `20-tool-system.md` + `50-versioning-model.md` | Tier 2/5 | A catalog tool, connection, or agent template: name, available versions, LATEST pointer, metadata. |
| `ArtifactRef` | `50-versioning-model.md` | Tier 5 | A versioned pointer: `{scope: catalog\|local, kind: tool\|connection\|agent_template, name: str, version: str}`. Resolvable to an on-disk directory. |

Agent runs produce `RunArtifact`s. Configs reference one another via `ConfigRef`. The meta-agent reads and writes specs.

## The lifecycle

From user intent to committed versioned artifact:

```
   ┌─────────────────────────────────────────────────────────────────────────┐
   │                                                                         │
   │   USER INTENT                                                           │
   │   "Build an agent that triages incoming exceptions"                     │
   │                                                                         │
   │                               │                                         │
   │                               ▼                                         │
   │   DEFINE          YAML + markdown written by user or meta-agent         │
   │   ─────────       (agents/recon/agent.yaml, prompts/v1.md, ...)         │
   │                               │                                         │
   │                               ▼                                         │
   │   VALIDATE        foundry.config.load(path) → Pydantic models           │
   │   ─────────       fails fast with structured error if invalid           │
   │                               │                                         │
   │                               ▼                                         │
   │   COMPILE         AgentSpec / SystemSpec → LangGraph StateGraph         │
   │   ─────────       via runtime adapter                                   │
   │                               │                                         │
   │                               ▼                                         │
   │   RUN             async execution over the StateGraph                   │
   │   ─────────       with checkpointer, interrupts, tracing                │
   │                               │                                         │
   │                               ▼                                         │
   │   CAPTURE         run emits RunArtifact + OTel trace                    │
   │   ─────────       stored under ~/.foundry/runs/<run_id>/                │
   │                               │                                         │
   │                               ▼                                         │
   │   EVAL            (optional) replay over an EvalSpec                    │
   │   ─────────       produces scored results + failure snippets            │
   │                               │                                         │
   │                               ▼                                         │
   │   VERSION         git commit of config changes                          │
   │   ─────────       meta-agent commits with structured message            │
   │                               │                                         │
   │                               ▼                                         │
   │   ROLLBACK        (on demand) `foundry rollback <ref>`                  │
   │   ─────────       atomically restores all configs to that git ref       │
   │                                                                         │
   └─────────────────────────────────────────────────────────────────────────┘
```

Every step is independently invokable from the CLI. The meta-agent drives this whole loop automatically when run as `foundry forge`, but each step is also a first-class CLI command for manual use and debugging.

## Dataflow during a run

```
    User / API caller
         │
         │  input dict
         ▼
   ┌─────────────────┐
   │ foundry.Session │  generates run_id, initializes trace, resolves configs
   └─────────────────┘
         │
         ▼
   ┌─────────────────┐
   │ CompiledSystem  │  LangGraph StateGraph (via adapter)
   └─────────────────┘
         │
         ▼
   ┌─────────────────┐       ┌──────────────┐       ┌──────────────┐
   │   node (agent)  │──────▶│ Provider API │──────▶│ LLM (remote) │
   │   async step    │◀──────│  call        │◀──────│              │
   └─────────────────┘       └──────────────┘       └──────────────┘
         │                           │
         │  tool call                │
         ▼                           │
   ┌─────────────────┐               │
   │   ToolRegistry  │               │
   │   dispatch      │               │
   └─────────────────┘               │
         │                           │
         │  tool result              │
         ▼                           │
   ┌─────────────────┐               │
   │  State update   │               │
   │  (Pydantic)     │               │
   └─────────────────┘               │
         │                           │
         ▼                           │
   ┌─────────────────┐               │
   │  Checkpointer   │               │  every step persisted
   └─────────────────┘               │
         │                           │
         │  next node or END         │
         ▼                           │
       ...                           │
         │                           │
         ▼                           ▼
   ┌─────────────────────────────────────┐
   │  RunArtifact + OTel trace           │
   │  stored in artifact store           │
   └─────────────────────────────────────┘
         │
         ▼
      caller
```

Every LLM call, tool call, and state transition is a span in the OTel trace and a record in the `RunArtifact`. Checkpointing happens after each node, so a killed process can resume from the last committed state.

## Runtime vs configurator — what each sees

### Runtime (what agents see when executing)

- `foundry.Session` — bound to a run id, threads trace + logger + checkpointer.
- `foundry.State` — Pydantic model for the system's state, scoped per node by the compiler.
- `foundry.Tool` — tool handlers, dispatched by `ToolRegistry`.
- `foundry.Provider` — LLM interface; returns `ModelResponse` objects.
- `foundry.errors` — `ApprovalRequired`, `ToolError`, `StateVisibilityError`, etc.

**Does not see:** Git, meta-agent, eval harness, anything under `foundry.configurator.*`.

### Configurator (what the meta-agent sees when configuring)

- `foundry.configurator.tools` — the full meta-toolkit (see the Meta-agent summary table below for the complete list).
- `foundry.configurator.MetaAgent` — the meta-agent class, itself a `foundry.Agent`.
- `foundry.catalog` — to enumerate catalog tools and templates.
- `foundry.eval` — to invoke the eval harness (per-tool, per-agent, project).
- `foundry.versioning` — to commit, list versions, rollback.

**Does not see:** Any running agent's memory, any non-foundry filesystem paths, `src/foundry/` source code, or projects outside the one it is scoped to. The meta-agent's `read_file` / `write_file` are sandboxed by absolute-path canonicalisation + prefix check against the scoped project directory.

## Multi-institution deployment pattern

`agent-foundry` is designed so multiple institutions can use the same framework while keeping their projects, institute-specific catalog items, prompts, eval sets, and audit logs fully private to their own repositories. The shape is a **three-layer overlay**: a shared upstream framework + public catalog, and private per-institution repos that overlay their own catalog + projects on top.

```
┌────────────────────────────────────────────────────────────────────┐
│  UPSTREAM FRAMEWORK  (public or shared-private)                    │
│  github.com/<owner>/agent-foundry                                  │
│                                                                    │
│    src/foundry/            the Python package                      │
│    docs/                   generic design docs                     │
│    catalog/public/         generic tools + connections:            │
│                              http_get, send_email_via_ses,         │
│                              query_postgres, slack_workspace,      │
│                              aws_session, azure_entra, ...         │
│    tests/                  framework tests                         │
│                                                                    │
│  Distributed as a pip/uv package: `uv add foundry`                 │
└─────────────────────────────┬──────────────────────────────────────┘
                              │  (dependency)
               ┌──────────────┴───────────────┐
               │                              │
               ▼                              ▼
┌─────────────────────────────┐  ┌─────────────────────────────┐
│  INSTITUTION A              │  │  INSTITUTION B              │
│  github.com/A/foundry-A     │  │  github.com/B/foundry-B     │
│  (private)                  │  │  (private)                  │
│                             │  │                             │
│  catalog/                   │  │  catalog/                   │
│   ├─ tools/                 │  │   ├─ tools/                 │
│   │   ├─ query_a_trade_db/  │  │   │   ├─ query_order_db/    │
│   │   └─ post_a_ticket/     │  │   │   └─ issue_refund/      │
│   └─ connections/           │  │   └─ connections/           │
│       ├─ a_snowflake/       │  │       ├─ orders_postgres/   │
│       └─ a_compliance_api/  │  │       └─ payments_api/      │
│                             │  │                             │
│  projects/                  │  │  projects/                  │
│   ├─ exception_triage/      │  │   ├─ refund_triage/         │
│   └─ compliance_surv/       │  │   └─ return_eligibility/    │
│                             │  │                             │
│  pyproject.toml             │  │  pyproject.toml             │
│    foundry ==1.3.0          │  │    foundry ==1.3.0          │
│                             │  │                             │
│  deploy/                    │  │  deploy/                    │
│    Dockerfile, k8s, etc.    │  │    Dockerfile, k8s, etc.    │
└─────────────────────────────┘  └─────────────────────────────┘

Institutions A and B cannot see each other's repos, catalogs,
projects, eval sets, or audit logs. Both pin `foundry==1.3.0`
and therefore share identical framework behaviour.
```

### Runtime root resolution

The runtime reads two environment variables to locate artifacts:

```
FOUNDRY_CATALOG_ROOTS="/app/foundry/catalog/public,/app/foundry-A/catalog"
FOUNDRY_PROJECTS_ROOT="/app/foundry-A/projects"
```

Catalog resolution walks roots left-to-right. The meta-agent's `write_file` sandbox is pinned to `FOUNDRY_PROJECTS_ROOT` + its scoped project directory — it cannot write to the framework package or to any catalog root.

Shadowing (private catalog overriding a public entry with the same name) is permitted but logged loudly at startup, so accidental shadowing is visible.

### What lives where (at-a-glance)

| Artifact | Upstream framework | Institution-private repo |
|---|---|---|
| `foundry` Python package | ✅ source of truth | pinned as dependency |
| Generic tools / connections | ✅ in `catalog/public/` | — |
| Institution-specific tools | ❌ | ✅ in `catalog/tools/` |
| Institution-specific connections | ❌ | ✅ in `catalog/connections/` |
| Projects (SystemSpec, agents, prompts) | ❌ | ✅ in `projects/` |
| Project-local tools / connections | ❌ | ✅ within each project |
| Eval sets (may contain regulated data) | ❌ | ✅ |
| Run artifacts, audit logs | ❌ | ✅ |
| Deploy manifests (Dockerfile, k8s, env) | — | ✅ |
| Credentials | ❌ (always out-of-band via SecretsProvider) | ❌ (ditto) |

### Contribution flow

- **Generic improvements** (framework bugs, new auth schemes, new public catalog tools that aren't institution-flavoured) go upstream via PR.
- **Institution-specific additions** stay in the institution's repo. Full stop.
- **Selective promotion**: if an institution builds something genuinely generic (say, a `query_postgres_read_replica` connection pattern), they can open a PR to move it into the public catalog. Human-reviewed, institution decides.

Full spec: `86-multi-tenancy-and-ip.md`.

## Directory layout (target shape of the repo)

```
agent-foundry/
├── pyproject.toml
├── uv.lock                         (committed)
├── .python-version                 (3.11 or 3.12)
├── README.md
├── docs/                           ← this tree
├── src/
│   └── foundry/
│       ├── __init__.py             public API re-exports
│       ├── core/
│       │   ├── agent.py            Agent protocol, BaseAgent
│       │   ├── tool.py             Tool protocol, BaseTool, ToolRegistry
│       │   ├── session.py          Session (run_id, trace, logger, checkpointer)
│       │   ├── state.py            State primitive, reducer types
│       │   ├── errors.py           shared exception hierarchy
│       │   └── types.py            shared Pydantic primitives
│       ├── providers/
│       │   ├── __init__.py         Provider, ModelBinding, capabilities
│       │   ├── anthropic.py
│       │   ├── openai.py
│       │   ├── bedrock.py
│       │   ├── azure.py
│       │   ├── vertex.py
│       │   └── _registry.py        lookup: name → provider class
│       ├── config/
│       │   ├── loader.py           YAML + Pydantic load
│       │   ├── schemas.py          AgentSpec, ToolSpec, SystemSpec, StateSpec
│       │   ├── composition.py      includes, overrides, env interpolation
│       │   ├── refs.py             ref resolution (catalog/name@version, local/name@version)
│       │   └── secrets.py          env + secrets provider interface
│       ├── catalog/
│       │   ├── loader.py           catalog index + version discovery
│       │   ├── promote.py          promote local → catalog (human-gated)
│       │   └── schemas.py          CatalogEntry, CatalogIndex
│       ├── auth/
│       │   ├── schemes/            per-scheme helpers: api_key, oauth2, sigv4, mtls, ...
│       │   ├── token_cache.py      short-lived token store w/ refresh
│       │   └── redactor.py         redact credentials in logs/traces
│       ├── connections/
│       │   ├── pool.py             ConnectionPool implementation
│       │   ├── registry.py         discovery + factory loading
│       │   ├── health.py           health-check runner
│       │   └── descriptors.py      ConnectionDescriptor builder
│       ├── cache/
│       │   ├── __init__.py         CacheAccessor composition
│       │   ├── semantic.py         SemanticCache implementations: in-process, redis, pgvector
│       │   ├── tool_result.py      ResultCache implementations
│       │   └── keys.py             stable hashing for cache keys
│       ├── retrieval/
│       │   ├── __init__.py         Retriever / Reranker registry
│       │   ├── dense.py            DenseRetriever (Embedder + vector store)
│       │   ├── sparse.py           SparseRetriever (BM25, vendor sparse)
│       │   ├── hybrid.py           HybridRetriever (RRF, weighted merge)
│       │   └── rerankers/          cross-encoder adapters (cohere, voyage, jina)
│       ├── memory/
│       │   ├── __init__.py         Memory coordinator factory + registry
│       │   ├── coordinator.py      DefaultMemory implementation (assembles envelopes)
│       │   ├── layers/
│       │   │   ├── working.py      WorkingMemoryLayer (state + window)
│       │   │   ├── episodic.py     EpisodicMemoryLayer (wraps Retriever)
│       │   │   └── semantic.py     SemanticMemoryLayer (state field + consolidator)
│       │   └── prompt_assembly.py  envelope → prompt-injection logic
│       ├── orchestration/
│       │   ├── compiler.py         SystemSpec → CompiledSystem (resolves refs + versions)
│       │   ├── patterns.py         supervisor, sequential, parallel, router
│       │   ├── state_scope.py      per-node visibility enforcement
│       │   └── hitl.py             interrupt/resume semantics
│       ├── runtime/
│       │   ├── langgraph_adapter.py   ONLY place LangGraph is imported
│       │   ├── _langgraph_types.py    LG type ↔ foundry type conversions
│       │   └── checkpointers.py       checkpointer selection (sqlite, pg, memory)
│       ├── eval/
│       │   ├── harness.py          runner (per-artifact + end-to-end)
│       │   ├── schemas.py          EvalSpec, EvalCase, EvalResult, EvalComparison
│       │   ├── compare.py          cross-version comparison
│       │   ├── scorers/
│       │   │   ├── exact.py
│       │   │   ├── llm_judge.py
│       │   │   └── rubric.py
│       │   └── reporter.py         formatted output for CLI and meta-agent
│       ├── versioning/
│       │   ├── git_backend.py      wrapped git operations
│       │   ├── artifacts.py        per-artifact version I/O (tools, prompts)
│       │   ├── pins.py             read/write version pins in system.yaml & agent.yaml
│       │   ├── rollback.py         per-artifact + per-project rollback
│       │   ├── audit.py            audit log writer/reader
│       │   └── refs.py             ConfigRef resolution
│       ├── configurator/
│       │   ├── meta_agent.py       MetaAgent class
│       │   ├── prompts/
│       │   │   └── v1.md
│       │   ├── tools/              meta-agent's toolkit
│       │   │   ├── fs.py           read_file, write_file
│       │   │   ├── registry.py     list_tools, list_agents, list_catalog, list_connections
│       │   │   ├── build.py        build_tool, build_agent, build_connection scaffolds
│       │   │   ├── eval.py         run_eval, read_eval_results, compare_versions
│       │   │   ├── git.py          git_commit, git_show, list_versions
│       │   │   ├── rollback.py     per-artifact rollback tool
│       │   │   └── connections.py  describe_connection, check_connection_health
│       │   └── session.py          interactive session orchestration
│       ├── api/
│       │   ├── app.py              FastAPI factory
│       │   ├── routes.py           generated endpoints per system
│       │   ├── streaming.py        SSE/websocket helpers
│       │   └── auth.py             auth plugin point (no v1 impl beyond bearer)
│       ├── observability/
│       │   ├── tracing.py          OTel setup, LangSmith optional exporter
│       │   ├── logging.py          structured logs with run_id
│       │   └── artifacts.py        RunArtifact writer
│       ├── storage/
│       │   ├── paths.py            filesystem layout (~/.foundry, etc.)
│       │   └── artifacts_store.py  runs and eval results storage
│       ├── cli/
│       │   ├── __main__.py         `python -m foundry`
│       │   ├── forge.py            meta-agent driver
│       │   ├── project.py          `foundry project new/list/diff`
│       │   ├── catalog.py          `foundry catalog list/promote/show`
│       │   ├── run.py              run a system
│       │   ├── serve.py            launch API
│       │   ├── eval.py             run evals + compare
│       │   ├── rollback.py         per-artifact + per-project rollback
│       │   └── tui/                (placeholder) lightweight review UI
│       └── security/
│           ├── sandbox.py          tool sandboxing
│           ├── injection.py        prompt-injection guardrails
│           └── validators.py       input/output validators
│
├── catalog/                        ← the upstream public catalog
│   └── public/                     ← generic artifacts shipped with the framework
│       ├── tools/
│       │   ├── http_get/v1/        generic HTTP GET with auth injection
│       │   ├── send_email_via_ses/v1/
│       │   ├── query_postgres/v1/  generic; accepts DSN via connection binding
│       │   ├── send_slack/v1/
│       │   └── escalate_generic/v1/
│       ├── connections/            shared authenticated handles to widely-used systems
│       │   ├── postgres/v1/        generic Postgres + OAuth2/mTLS
│       │   ├── pgvector/v1/        Postgres with pgvector for RAG
│       │   ├── pinecone/v1/        managed vector store
│       │   ├── qdrant/v1/          open-source vector store
│       │   ├── elasticsearch/v1/   lexical / sparse search
│       │   ├── cohere_rerank/v1/   rerank service
│       │   ├── voyage_embed/v1/    embedding service
│       │   ├── slack_workspace/v1/
│       │   ├── aws_session/v1/     SigV4 / assume-role / SSO
│       │   ├── azure_entra/v1/     managed identity / service principal
│       │   ├── gmail_oauth/v1/
│       │   └── github_app/v1/
│       ├── retrievers/             reusable retriever templates
│       │   ├── pgvector_dense/v1/  dense-only pgvector
│       │   ├── elastic_bm25/v1/    sparse-only Elasticsearch
│       │   └── hybrid_rrf/v1/      composed dense + sparse with RRF
│       ├── agent_templates/        (optional; empty until needed)
│       │   └── summarizer/v1/…
│       └── index.yaml              catalog index for this root
│
│                                   Institution-specific catalogs (`query_internal_trades`,
│                                   `corp_snowflake`, etc.) live in a separate private
│                                   repo with its own catalog/ tree mounted as an additional
│                                   catalog root via FOUNDRY_CATALOG_ROOTS. See
│                                   `86-multi-tenancy-and-ip.md` for the overlay model.
│
├── projects/                       ← institution-specific: configured multi-agent systems
│                                   (in multi-institution deployments, this tree lives in
│                                   the institution's private repo, not in the upstream;
│                                   see `86-multi-tenancy-and-ip.md`)
│   └── <project_name>/
│       ├── system.yaml             SystemSpec — pins tool and prompt versions (the manifest)
│       ├── state.yaml              StateSpec
│       ├── agents/
│       │   └── <agent_name>/
│       │       ├── agent.yaml      AgentSpec — pins prompt version, declares tools + state scope
│       │       ├── prompts/
│       │       │   ├── v1.md
│       │       │   ├── v2.md
│       │       │   └── …           (numbered; agent.yaml pins which is live)
│       │       └── output_schema.py
│       ├── tools/                  project-LOCAL tools (not promoted to catalog)
│       │   └── <tool_name>/
│       │       ├── v1/             same 5-file shape as catalog tools
│       │       │   ├── tool.yaml
│       │       │   ├── handler.py
│       │       │   ├── schemas.py
│       │       │   ├── eval.yaml
│       │       │   └── README.md
│       │       ├── v2/…
│       │       └── versions.json
│       ├── connections/            project-LOCAL connections (internal systems w/o catalog presence)
│       │   └── <connection_name>/
│       │       ├── v1/             same shape as catalog connections
│       │       │   ├── connection.yaml
│       │       │   ├── auth.py
│       │       │   ├── schemas.py
│       │       │   ├── health.yaml
│       │       │   └── README.md
│       │       └── versions.json
│       ├── evals/
│       │   └── <eval_name>.yaml    end-to-end project EvalSpec
│       └── .foundry/               per-project metadata (audit log, run index)
│
├── tests/
│   ├── unit/
│   ├── integration/
│   └── contract/                   contracts against LangGraph behaviour
└── personal_docs/                  (gitignored — seed + scratch)
```

Three top-level trees the meta-agent cares about:

- **`src/foundry/`** — the developer kit itself. The meta-agent's `write_file` tool CANNOT write here (sandbox rejects).
- **`catalog/`** — shared, versioned library of tools (and optionally agent templates) reusable across projects. The meta-agent can READ but cannot write here directly. Writing requires `foundry catalog promote` — a human-gated action — because catalog changes ripple across every project that pins them.
- **`projects/`** — the configured multi-agent systems. The meta-agent has full write access, scoped to the project it's working on (can't cross-write between projects without explicit `--project` flag).

This gives you: shared tools without the meta-agent accidentally editing them; per-project isolation; a clear promotion path when a project-local tool proves broadly useful.

## Module ownership and dependency rules

```
         ┌─────────────────────────────────────────────────────────────┐
         │                                                             │
         │      cli/  ←──  configurator/  ←──  api/                    │
         │        │            │                │                      │
         │        └──────┬─────┴────────┬───────┘                      │
         │               ▼              ▼                              │
         │           eval/  ←───  versioning/                          │
         │               │              │                              │
         │               ▼              ▼                              │
         │          orchestration/  observability/                     │
         │               │              │                              │
         │               ▼              │                              │
         │            runtime/          │                              │
         │               │              │                              │
         │               ▼              ▼                              │
         │            config/  ←────  storage/  ←────  security/       │
         │               │              │                              │
         │               └──────┬───────┘                              │
         │                      ▼                                      │
         │                   providers/                                │
         │                      │                                      │
         │                      ▼                                      │
         │                    core/                                    │
         │                                                             │
         └─────────────────────────────────────────────────────────────┘
```

Hard rules, enforced by `ruff` import-boundary config:

1. **`core/` imports nothing foundry-internal** besides stdlib and `pydantic`.
2. **`runtime/langgraph_adapter.py` is the only module that imports `langgraph` or `langchain_*`.** Other modules that need LangGraph-like functionality go through the adapter's public interface.
3. **`configurator/` is the ONLY consumer of `eval/` + `versioning/` as a unit.** API and direct CLI commands can use eval and versioning, but the meta-agent composes them.
4. **`api/` does not import `configurator/`.** The configurator is dev-time; API is run-time. If an API endpoint needs to call the configurator, that's a separate admin API (not in v1).

## Config format and directory conventions

Every user-facing config is YAML. Every prompt is markdown. Every output schema is Python (Pydantic model). Every tool handler is Python.

### File shape

| Path | Type | Purpose |
|---|---|---|
| `projects/<name>/system.yaml` | `SystemSpec` | The project manifest: agents, flow, state ref, tool version pins. |
| `projects/<name>/state.yaml` | `StateSpec` | State schema + per-agent visibility rules. |
| `projects/<name>/agents/<agent>/agent.yaml` | `AgentSpec` | Agent identity, model binding, prompt pin, tool allowlist, state scope. |
| `projects/<name>/agents/<agent>/prompts/v<N>.md` | prompt | Versioned prompt; agent.yaml pins which is live. |
| `projects/<name>/agents/<agent>/output_schema.py` | Pydantic module | Output schema for this agent (single file, git-versioned). |
| `projects/<name>/tools/<tool>/v<N>/tool.yaml` | `ToolSpec` | Project-local tool version. |
| `projects/<name>/tools/<tool>/v<N>/{handler.py, schemas.py, eval.yaml, README.md}` | tool files | The five standard tool files, one set per version. |
| `projects/<name>/evals/<name>.yaml` | `EvalSpec` | End-to-end project eval. |
| `catalog/tools/<tool>/v<N>/...` | same five files | Catalog tool version — same shape as local. |
| `catalog/tools/<tool>/LATEST` | text | One-line file naming the recommended version (e.g. `v3`). |
| `catalog/tools/<tool>/versions.json` | json | Metadata: versions, eval scores, deprecations. |

### Version axes (summary, detail in `50`)

- **Tool versioning** is directory-based (`v1/`, `v2/`, …). Version pin lives in `system.yaml`. Applies to both catalog and project-local tools.
- **Prompt versioning** is file-based (`v1.md`, `v2.md`, …). Version pin lives in `agent.yaml`.
- **Everything else** is git-versioned only. Rollback uses `git checkout <path>` under the hood.

No `current` symlinks — pins are explicit in YAML. This keeps everything diff-legible and avoids filesystem state that isn't tracked by git.

### Ref format

Refs to versioned artifacts use a canonical string form everywhere:

```
<scope>/<kind>/<name>@<version>
catalog/query_snowflake@v2
local/validate_deltas@v3
catalog/agent_templates/summarizer@v1
```

Parsed into an `ArtifactRef` Pydantic model. Resolver maps to an on-disk path. Used in CLI (`foundry rollback --tool catalog/query_snowflake@v2`), in meta-agent tool calls, and in the audit log.

## Provider abstraction summary

All provider-specific quirks live in `foundry/providers/`. Upper layers see a single `Provider` interface:

```python
class Provider(Protocol):
    name: str
    capabilities: ProviderCapabilities

    async def generate(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema],
        settings: ModelSettings,
    ) -> ModelResponse: ...

    async def stream(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema],
        settings: ModelSettings,
    ) -> AsyncIterator[ModelDelta]: ...
```

A `ModelBinding` (provider name + model id + settings) is everything the foundry needs to locate and call a model. Agents reference `ModelBinding` by config; the resolver in `providers/_registry.py` maps to a concrete `Provider` instance. Swapping providers means editing the binding — no other change in the agent config. See `11-provider-abstraction.md`.

## State management summary

State is a Pydantic model, declared in `state.yaml` (which is parsed into a `StateSpec`). Per-node visibility is declared in `state.yaml` too, keyed by agent name:

```yaml
# projects/<project_name>/state.yaml
schema:
  messages: list[Message]
  draft_plan: str | None
  final_report: str | None
  tool_scratchpad: dict[str, Any]

reducers:
  messages: append
  tool_scratchpad: merge
  # others default to last-write-wins

visibility:
  supervisor:
    read: [messages, draft_plan, final_report]
    write: [final_report]
  worker_planner:
    read: [messages]
    write: [draft_plan, tool_scratchpad]
  worker_writer:
    read: [messages, draft_plan]
    write: [final_report, tool_scratchpad]
```

The compiler enforces this by generating LangGraph subgraphs with input/output schemas matching the `read`/`write` lists. A worker cannot even *see* fields it doesn't have `read` access to — they are projected out before the node runs. Detail in `22-state-management.md`.

## Versioning summary

The foundry uses **three independent versioning axes**, each with appropriate granularity:

| Axis | Granularity | Mechanism | Rollback |
|---|---|---|---|
| **Tools** (catalog + local) | Directory-per-version: `tools/<name>/v1/`, `v2/`, … | Each version's files are immutable once committed. `system.yaml` pins which version the project uses. | Edit `system.yaml`, change `version: v3` → `v2`, commit. Old version stays on disk. |
| **Prompts** (per agent) | File-per-version: `prompts/v1.md`, `v2.md`, … | `agent.yaml` pins which prompt version is live. | Edit `agent.yaml`, change `prompt: v5` → `v4`, commit. |
| **Everything else** (system.yaml, state.yaml, agent.yaml, output_schema.py, handler.py bodies *within* a tool version) | Single-file git history | Standard git commits on the project's branch `foundry/<project_name>`. | `git checkout <commit> -- <path>` + commit, or `foundry rollback --project <name>` for the whole tree. |

**Why this layered model:** shareable, standalone artifacts (tools, prompts) deserve an explicit directory-based version axis because you want to compare iterations, pin exact versions across projects, and see versions with `ls`. Compositions (an agent, a whole system) have no single "version" — their identity is the combination of their pinned children, which git already represents. Mixing both axes gives you surgical per-artifact rollback *and* project-wide snapshot rollback when you want it.

**Version pins in `system.yaml`** are the manifest:

```yaml
# projects/pipeline_recon/system.yaml (abridged)
tools:
  validate_deltas:
    ref: local/validate_deltas
    version: v3
  query_snowflake:
    ref: catalog/query_snowflake
    version: v2
```

Rollback a single tool: meta-agent (or human) edits `v3 → v2` and commits. Other tools unchanged. Full detail in `50-versioning-model.md`.

**Eval comparison across versions** is a first-class workflow built on this structure:
- Per-artifact: `foundry eval compare --tool validate_deltas v1 v2 v3` runs the tool's standalone `eval.yaml` against each version.
- End-to-end (version bisect): `foundry eval compare --project pipeline_recon --pin-set current --pin-set HEAD~5` runs the project eval against two version-pin configurations and reports the delta per agent.

This lets you isolate the impact of a single tool change in full-system context — hold all pins constant except one.

**Audit log** (`.foundry/audit.jsonl` per project) records: commit sha, meta-agent run id, eval score before/after, human reviewer, artifact affected. Queryable without shelling out to git. Detail in `50-52`.

## Eval summary

An `EvalSpec` is a YAML file listing cases (input + expected output + metadata), a list of scorers, a pass threshold, and iteration config. The harness runs the agent system (compiled with a specific config) against each case, scores outputs, and produces a `EvalRunResult` — stored as a run artifact and readable by the meta-agent via `read_eval_results`. Scorers: `exact`, `llm_judge`, `rubric`, and user-plugged. Detail in `40`.

## Meta-agent summary

The meta-agent is itself an agent, defined by the foundry, with a specific toolkit:

| Tool | Domain | What it does |
|---|---|---|
| `read_file` | filesystem | Read any file under `projects/<scoped_project>/` or `catalog/`. Sandboxed. |
| `write_file` | filesystem | Write a config or prompt under the scoped project only. Refuses writes to `catalog/` and `src/foundry/`. |
| `list_catalog` | catalog | Returns all catalog tools, connections, and agent templates with their available versions and LATEST pointers. |
| `list_tools` | registry | Returns tools currently used by the scoped project (with pinned versions) + all catalog tools available. |
| `list_connections` | registry | Returns connection bindings in the scoped project + all catalog connections. Indicates which auth schemes each supports. |
| `list_agents` | registry | Returns agents in the scoped project + agent templates available. |
| `describe_connection` | connections | Returns a ConnectionDescriptor for a bound connection (redacted config, auth scheme, principal) — safe for meta-agent reasoning. |
| `check_connection_health` | connections | Runs the connection's health-check EvalSpec; returns pass/fail + latency. Called before committing a new binding. |
| `build_tool` | scaffold | Scaffolds a new project-local tool version (`v1/`): tool.yaml + handler.py + schemas.py + eval.yaml + README.md. If the tool needs an external system, the meta-agent first ensures a ConnectionBinding exists (calling `build_connection` if needed) before wiring the slot. |
| `build_connection` | scaffold | Scaffolds a new project-local connection version (`v1/`): connection.yaml + auth.py + schemas.py + health.yaml + README.md. The meta-agent fills in factory body for a declared auth_scheme, runs health check, iterates if needed. |
| `build_agent` | scaffold | Scaffolds a new agent directory: agent.yaml + prompts/v1.md + output_schema.py. |
| `new_prompt_version` | scaffold | Creates `prompts/v<N+1>.md` copying from the current pinned version as a starting point. |
| `pin_version` | versioning | Updates the pinned version in system.yaml or agent.yaml. |
| `run_eval` | eval | Runs a per-tool, per-agent, or project-level `EvalSpec`. |
| `read_eval_results` | eval | Reads a specific run's eval artifact. |
| `compare_versions` | eval | Runs the same eval across multiple versions of an artifact or multiple pin-sets; returns a comparison. |
| `git_commit` | versioning | Commits pending changes with a structured message including the artifact affected. |
| `git_show` | versioning | Shows a diff for a commit. |
| `list_versions` | versioning | Lists versions for a specific tool or prompt (directory or file enumeration), or commits on the project branch. |
| `rollback` | versioning | Per-artifact rollback by updating the relevant pin. Atomic at the YAML-edit + commit level. |

The meta-agent's prompt tells it: discover what exists (catalog + local), design the system, scaffold what's missing, run eval, read failures, iterate until threshold or max iterations. It does NOT contain hard-coded "if X then Y" improvement heuristics. The LLM IS the heuristic.

**Catalog promotion is NOT a meta-agent tool.** Promoting a project-local tool to the catalog is a deliberate human action (`foundry catalog promote`) because it affects every project that might pin it. The meta-agent can suggest promotion in its summary; the human executes. See `60-meta-agent.md`.

## API layer summary

`foundry serve <project>` launches a FastAPI app with endpoints generated from the project's config. A minimal `SystemSpec` produces:

- `POST /run` — non-streaming run, returns run id + final output.
- `POST /stream` — streaming run (SSE), progressive `RunEvent`s.
- `POST /batch` — submit many runs with a shared batch_id, streamed per-item results. See `85-batch-and-throughput.md`.
- `WS /ws` — bidirectional streaming. Server writes `RunEvent`s; client writes `InboundMessage`s (inject input, approval response, cancel, pause/resume).
- `GET /runs/{run_id}` — run status and (when complete) artifact URL.
- `GET /runs/{run_id}/events` — replay the `RunEvent` stream from a persisted artifact (supports `?from_sequence=N` for resume).
- `POST /runs/{run_id}/resume` — resume an interrupted run (HITL approval response).
- `GET /health` — health check; aggregates connection healths.
- `GET /config` — read-only view of the compiled config (minus secrets).

Auth is a plug-point (bearer token validator in v1). See `70-api-layer.md` (wire format detail) and `85-batch-and-throughput.md` (scale-out, rate limiting, batch semantics).

## Streaming summary

Every run emits a progressive stream of typed `RunEvent`s (`run.started`, `agent.started`, `llm.delta`, `tool.started`, `tool.completed`, `connection`, `handoff`, `state.transition`, `approval.required`, `run.completed`, `run.failed`, `run.cancelled`). The event union is defined in `10-core-framework.md`; it's the spine that every streaming surface consumes.

### Event flow

```
provider adapter yields ModelDelta
     │
     ▼
agent's astream() (default: synthesised around run() via hooks)
     │  emits LLMCallStarted / LLMDelta / ToolStarted / ToolCompleted / LLMCallCompleted / AgentCompleted
     ▼
orchestration runtime multiplexes events from all active nodes
     │  adds Handoff, StateTransition, ApprovalRequired events
     ▼
session.event_sink (one subscriber per streaming consumer)
     │
     ├──▶ SSE encoder  → text/event-stream → HTTP client
     ├──▶ WebSocket encoder → JSON frames → WebSocket client
     ├──▶ CLI --stream renderer → terminal
     ├──▶ run artifact writer → trace.jsonl
     └──▶ OTel exporter → backend
```

Same event type, different encoders. A new streaming surface (e.g. a future TUI dashboard) subscribes to the event sink without changes in the runtime.

### SSE envelope

Standard `text/event-stream`. Each event carries `id: <sequence>`, `event: <event discriminator>`, `data: <JSON>`. Clients reconnect with `Last-Event-ID` → server replays from the persisted run artifact.

### WebSocket envelope

Bidirectional JSON frames:
- Outbound: `{"direction":"outbound", "event": <RunEvent>}`
- Inbound: `{"direction":"inbound",  "message": <InboundMessage>}`

Inbound messages the client can send: `InjectInput` (append a message to conversation state), `ApprovalResponse` (resolve a pending `ApprovalRequired`), `CancelRun`, `PauseRun`, `ResumeRun`.

Use SSE when the flow is request-response-streamed (most use cases — batch, triage, one-shot). Use WebSocket when the flow is interactive (human-in-the-loop approval UIs, chat-style sessions, ops consoles injecting inputs mid-run).

### Invariants (normative)

- `sequence` is strictly monotonic within a run. No gaps in persistence; gaps only arise from network drops, and clients MUST reconnect with `Last-Event-ID`.
- A run's event stream begins with exactly one `run.started` and ends with exactly one of `run.completed` | `run.failed` | `run.cancelled`.
- Every `*.started` event has a matching `*.completed` event (or `run.failed` if the run died before completion).
- Event `data` respects `ObservabilityConfig.capture_*` flags — previews are `None` when suppression is on.

## Observability summary

Observability is built as an **audit trail first, monitoring source second, debugging aid third**. Every run emits structured events with dimensions suitable for aggregation — not just free-text for humans to skim.

### Three transport layers (all always-on except LangSmith)

- **OTel traces** — spans: `foundry.run`, `foundry.node`, `foundry.llm`, `foundry.tool`, `foundry.handoff`, `foundry.state_transition`. Attributes enumerated below. Exported via OTLP; configurable backend.
- **OTel metrics** — counters (`foundry.llm.calls_total`, `foundry.tool.calls_total`, `foundry.handoff_total`), histograms (`foundry.llm.latency_ms`, `foundry.tool.latency_ms`), gauges. Tagged by dimensions so they're aggregatable by provider, model, tool, agent, project.
- **Local queryable store** — SQLite at `~/.foundry/observability.db` mirrors the event stream for cross-run queries during dev. Schema covers runs, llm_calls, tool_calls, handoffs, evals. `foundry obs` CLI exposes common queries (cost per project, tool failure rates, p95 latency per model).

LangSmith integration is **opt-in** (`FOUNDRY_TRACING=langsmith`). OTel + SQLite are always-on.

### Event attribute spec (enforced at the instrumentation layer)

| Event | Required attributes |
|---|---|
| `foundry.run` | `run_id`, `project`, `system_version` (git sha), `pin_set_hash`, `started_at`, `status`, `total_duration_ms`, `total_cost_estimate_usd`, `total_input_tokens`, `total_output_tokens`, `worker_id` (hostname+pid), `batch_id` (if submitted as part of a batch) |
| `foundry.llm` | `run_id`, `agent`, `provider`, `model`, `prompt_tokens`, `completion_tokens`, `cached_read_tokens`, `cache_write_tokens`, `latency_ms`, `cost_estimate_usd`, `temperature`, `max_tokens`, `tool_schemas_count`, `stop_reason`, `error` |
| `foundry.tool` | `run_id`, `agent`, `tool_ref`, `tool_version`, `input_hash`, `output_hash`, `success`, `latency_ms`, `retry_count`, `error_category`, `connections_used` (list of ConnectionDescriptor refs) |
| `foundry.connection` | `run_id`, `connection_ref`, `connection_version`, `slot`, `auth_scheme`, `principal` (redacted), `event` (`acquire`/`cache_hit`/`refresh`/`release`/`evict`), `latency_ms`, `config_hash`, `error_category` |
| `foundry.embed` | `run_id`, `agent`, `embedder`, `model`, `input_count`, `input_tokens`, `purpose` (`query`/`document`), `latency_ms`, `cost_estimate_usd` |
| `foundry.cache.semantic` | `run_id`, `agent`, `event` (`hit`/`miss`/`store`/`invalidate`), `similarity` (hit/miss), `threshold`, `cached_at` (hit), `saved_tokens_estimate` (hit), `saved_cost_usd` (hit), `ttl_s` (store) |
| `foundry.cache.tool` | `run_id`, `agent`, `tool_ref`, `tool_version`, `event` (`hit`/`miss`/`store`), `cached_at` (hit), `input_hash` |
| `foundry.retrieval` | `run_id`, `agent`, `retriever`, `kind` (`dense`/`sparse`/`hybrid`), `top_k`, `returned`, `latency_ms` |
| `foundry.rerank` | `run_id`, `agent`, `reranker`, `model`, `candidates`, `top_k`, `latency_ms`, `cost_estimate_usd` |
| `foundry.memory.read` | `run_id`, `agent`, `layers_read` (list), `layers_failed` (list), `total_tokens_estimate`, `truncated` |
| `foundry.memory.write` | `run_id`, `agent`, `layer_name`, `layer_kind`, `write_kind`, `bytes` |
| `foundry.memory.consolidate` | `run_id`, `agent`, `layer_name`, `trigger` (`periodic`/`session_end`/`explicit`), `input_tokens_summarised`, `output_tokens_written`, `latency_ms` |
| `foundry.handoff` | `run_id`, `from_agent`, `to_agent`, `trigger` (`rule`/`llm`/`end`), `hop_number`, `state_size_bytes` |
| `foundry.state_transition` | `run_id`, `agent`, `fields_written`, `bytes_delta` |
| `foundry.eval` | `eval_run_id`, `project`, `eval_spec_ref`, `pin_set_hash`, `cases_total`, `cases_passed`, `score`, `per_case_results_ref` |

Attribute shape is frozen; additions are allowed, removals/renames require a major foundry version bump. This is what makes the audit trail usable as a monitoring source later — downstream dashboards can rely on the schema.

### Run artifact (per-run offline record)

JSONL under `~/.foundry/runs/<run_id>/`: `meta.json`, `trace.jsonl`, `inputs.json`, `outputs.json`, `state_transitions.jsonl`, `llm_calls.jsonl`, `tool_calls.jsonl`. Redundant with the OTel stream but portable — a run artifact can be zipped, sent to a reviewer, replayed.

### What this enables downstream

- Monitoring dashboards for deployed projects — point a Prometheus / Datadog / Langfuse exporter at the OTel stream; no instrumentation changes required.
- Cost attribution per project / per agent / per model over any time window.
- Tool-reliability metrics (which tools fail, retry counts, p95 latencies).
- Drift detection — is this week's cost-per-run deviating from last month's?
- Handoff-pattern analysis — is the supervisor routing the same way for the same state shapes?

Detail in `80-observability.md`.

## Concurrency model

### Within a process

- **Single event loop per process.** The foundry does not spawn loops internally. CLI entrypoints use `asyncio.run`; FastAPI uses uvicorn's loop; notebooks use Jupyter's.
- **Blocking code in an async context is a bug.** Tool handlers that need to do CPU work must explicitly `await asyncio.to_thread(...)` or use `concurrent.futures`.
- **Parallel agent nodes** are expressed via LangGraph's `Send` API under the hood; exposed in foundry config as `flow: { type: parallel, nodes: [...] }`. They run concurrently in the same loop.
- **Long runs** are supported via checkpointing. A run that takes hours can be interrupted, the process killed, and resumed from the last checkpoint on a new process.

### Scaling topology

```
   single loop            multi-worker              multi-host
   ───────────            ────────────              ──────────
   1 process              N processes (uvicorn)     N hosts × M workers
   1 pool                 N pools                   N × M pools
   1 token cache          N token caches            N × M token caches
   shared: OTel,          shared: OTel + Postgres   + shared: Redis rate limiter,
   audit store              checkpointer + audit       cross-host run registry,
                            store                       batch budget counter

   ~100 concurrent runs   ~500–2000 concurrent      10,000+ concurrent
   dev, smoke tests       batch, real-time triage   large batch, multi-region
```

The foundry targets **multi-worker single-host** as the default production shape. Multi-host is supported for scale-out but requires the shared-state primitives (Redis-backed rate limiter, Postgres checkpointer, shared audit store) that `85-batch-and-throughput.md` specifies.

Per-process vs shared state (important when you're sizing and deploying):

| Per-process | Shared across processes |
|---|---|
| `ConnectionPool` instances | Postgres checkpointer |
| Token caches in `foundry.auth` | OTel collector backend |
| In-memory provider rate-limiter (dev) | Redis-backed rate-limiter (prod) |
| `ConnectionDescriptor` redactor state | SQLite/Postgres audit-event mirror |
| LangGraph graph objects | Run artifact store (filesystem or object store) |

### Streaming and multi-worker

Runs on SSE or WebSocket have implicit worker affinity — the socket attaches to whichever worker accepted the request. Two consequences:

- **SSE clients must support `Last-Event-ID`.** If the accepting worker dies mid-run, the checkpointer holds run state; a reconnecting client can be routed to any worker, which replays from `sequence = Last-Event-ID + 1` using the persisted `RunEvent` history.
- **WebSocket is sticky by `run_id`.** Inbound messages (`approval_response`, `inject_input`, `cancel`) must reach the worker currently running the loop. Achieved by load-balancer hashing `run_id` or by a lightweight `run_id → worker` registry (Redis hash). WebSocket clients on reconnect may land on a new worker; they must be prepared to resume via SSE-style replay if the originating worker is gone.

Full detail in `85-batch-and-throughput.md`.

## Security-boundary summary

- **Meta-agent is sandboxed.** Its `write_file` cannot escape `agents/`. Paths are resolved absolute and compared against a canonical root.
- **Tools are allowlisted per agent.** An `AgentSpec` lists the tools it may use. The `ToolRegistry` enforces. An agent cannot call a tool not in its allowlist even if the model names it.
- **Provider keys live in environment variables or a secrets provider** — never in YAML. The config loader rejects any key that looks like a secret literal.
- **Prompt-injection guardrails** apply to tool-output text interpolation. Output from external systems (HTTP APIs, databases) is demarcated in prompts with a typed boundary. See `83-security-guardrails.md`.

## Minimal end-to-end example (conceptual)

Here is the life of a simple agent, to ground every abstraction above.

```yaml
# projects/hello/system.yaml
name: hello
description: "Single-agent sanity check."
agents: [hello_agent]
state: state.yaml
flow:
  type: single
  agent: hello_agent
tools: {}            # no external tools in this trivial example
guardrails:
  max_iterations: 4
observability:
  trace: otel
```

```yaml
# projects/hello/agents/hello_agent/agent.yaml
name: hello_agent
model_binding:
  provider: anthropic
  model: claude-opus-4-7
  settings:
    max_tokens: 1024
    temperature: 0.2
prompt:
  version: v1                      # pins which prompt file is live
  path: prompts/v1.md
output:
  schema: output_schema.py::Greeting
tools: []
state_visibility:
  read: [messages]
  write: [messages, final_greeting]
```

```yaml
# projects/hello/state.yaml
schema:
  messages: list[Message]
  final_greeting: str | None
reducers:
  messages: append
visibility:
  hello_agent:
    read: [messages]
    write: [messages, final_greeting]
```

```markdown
<!-- projects/hello/agents/hello_agent/prompts/v1.md -->
You are a polite assistant. Greet the user warmly in one sentence.
```

```python
# projects/hello/agents/hello_agent/output_schema.py
from pydantic import BaseModel

class Greeting(BaseModel):
    text: str
    language: str
```

From the CLI:

```
$ foundry run hello --input '{"messages": [{"role":"user","content":"hi"}]}'
[foundry] run_id=01JKM4... project=hello
[foundry] resolving pins... agent hello_agent prompt=v1
[foundry] compiling... OK
[foundry] executing hello_agent... OK (1 model call, 0 tool calls, 421ms)
[foundry] run artifact: ~/.foundry/runs/01JKM4.../
{"text": "Hello! Lovely to meet you.", "language": "en"}
```

Swapping providers is a single-field edit in `agent.yaml`:

```yaml
model_binding:
  provider: openai
  model: gpt-5
  settings:
    max_tokens: 1024
    temperature: 0.2
```

No other file changes. No Python changes. `foundry run hello ...` works identically.

### Example with a catalog tool

A slightly more realistic slice, showing a catalog tool being pinned and used:

```yaml
# projects/pipeline_recon/system.yaml (abridged)
name: pipeline_recon
agents: [supervisor, break_detector, resolver]
state: state.yaml
flow:
  type: supervisor
  supervisor: supervisor
  workers: [break_detector, resolver]
tools:
  query_snowflake:
    ref: catalog/query_snowflake
    version: v2                    # ← pinned; catalog can have newer but we stay here
  send_slack:
    ref: catalog/send_slack
    version: v1
  validate_deltas:
    ref: local/validate_deltas
    version: v3                    # ← project-local; lives in projects/pipeline_recon/tools/
```

```yaml
# projects/pipeline_recon/agents/break_detector/agent.yaml (abridged)
name: break_detector
model_binding:
  provider: anthropic
  model: claude-opus-4-7
prompt:
  version: v4
  path: prompts/v4.md
tools: [query_snowflake, validate_deltas]   # names only; versions resolved via system.yaml
state_visibility:
  read: [messages, current_break]
  write: [messages, detected_breaks]
```

Rolling back just the validator after a regression:

```
$ foundry eval compare --tool validate_deltas v1 v2 v3
v3 dropped to 0.71 accuracy (v2 was 0.92). Suspected bad change.

$ foundry rollback pipeline_recon --tool validate_deltas --to v2
Updated projects/pipeline_recon/system.yaml (v3 → v2)
Committed: "rollback validate_deltas v3→v2" on foundry/pipeline_recon
```

v3's files remain on disk — you can roll forward with `--to v3` later. No other agents or tools were touched.

## Where each layer starts

If you are implementing the foundry from scratch, this is the slice order. Each slice produces something runnable.

1. Core primitives + provider abstraction + config loader. Smoke test: `foundry run` on a trivial single-agent project against Anthropic.
2. Tool system + catalog layout + per-tool versioning directories + state management. Smoke test: the trivial agent calls both a catalog tool and a project-local tool; each is pinned by version in system.yaml.
3. Orchestration compiler (single-agent first). Smoke test: state visibility config is enforced — a violation raises at compile time; `ArtifactRef`s resolve correctly.
4. Eval harness, per-artifact and end-to-end, with `compare_versions`. Smoke test: compare tool v1 vs v2 yields a delta report.
5. Versioning + git backbone + per-artifact rollback. Smoke test: `foundry rollback <project> --tool <name> --to <version>` restores the pin atomically without touching other artifacts.
6. Meta-agent including `build_tool` and `build_agent` scaffolds. Smoke test: `foundry forge` loops to threshold on a toy task and scaffolds a missing tool.
7. Multi-agent orchestration + HITL. Smoke test: supervisor+worker with scoped state; interrupt + resume.
8. API layer. Smoke test: `foundry serve` + HTTP round-trip.
9. Observability + dev UX (review/rollback TUI) + security. Smoke test: trace exported; TUI lists per-artifact versions and triggers a rollback; prompt-injection guardrail test passes.

Mapping to phases in `03-development-phases.md`.

## Open questions

- **Artifact retention.** How long do we keep run artifacts by default? Proposal: 30 days in `~/.foundry/runs/`, archive older to a gzip tarball. TBD in `81`.
- **Cost tracking.** Should `RunArtifact` include a token + $ cost estimate? Yes in v1 — every provider reports tokens; the `ProviderCapabilities` table in `11` will include per-model pricing (configurable, user-overridable).
- **Config composition.** How deeply does `includes` / `extends` go? Proposal: shallow merge with override precedence; no recursive includes. TBD in `12`.
- **Notebook UX.** Is `from foundry import ...` + `await system.run(...)` the notebook path? Yes. The CLI is one consumer of the same library; notebooks are another. No duplicate code paths. Detailed in `82`.
