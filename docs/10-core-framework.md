# 10 — Core Framework

## Purpose

The core framework is the small, opinionated set of primitives that every other layer depends on. It defines the *types and contracts* — what an agent is, what a tool is, what a message is, how a run is scoped, what exceptions everyone agrees on. Nothing in this doc knows about YAML, catalogs, evals, versioning, or LangGraph. Those layers consume what's defined here; they do not influence its shape.

This doc is the interface contract for the foundry. If this is wrong, everything downstream is wrong.

## Pinned dependencies

The runtime + dev dependency pins this tier relies on (Pydantic v2, `anyio`, `structlog`, OTel triple, etc.) live in [`pyproject.toml`](../pyproject.toml). Per-pin rationale — version bands, exact-pin reasons (`langgraph`, `langchain-core`, OTel triple), and CLI-framework choice — is documented in the leading comment block of `pyproject.toml` and in [`docs/_phase_handoffs/phase_0.md`](_phase_handoffs/phase_0.md) (Phase 0 chose them; subsequent Tier 1 sub-docs will expand this section as primitives land).

## Module boundaries

```
src/foundry/core/
├── __init__.py      public surface (small, curated re-exports)
├── node.py          Node protocol (common parent of Agent and FunctionNode)
├── agent.py         Agent protocol, BaseAgent, LifecycleHooks
├── function_node.py FunctionNode protocol, BaseFunctionNode (deterministic Python nodes)
├── tool.py          Tool protocol, BaseTool, ToolRegistry, RunContext
├── connection.py    Connection protocol, ConnectionPool, ConnectionAccessor, ConnectionFactory
├── embedder.py      Embedder protocol, Embedding, EmbedderCapabilities
├── retrieval.py     Retriever, Reranker protocols, RetrievedDocument
├── memory.py        Memory, MemoryLayer protocols, MemoryEnvelope, MemoryContribution, MemoryWrite
├── cache.py         SemanticCache, SemanticCacheKey, SemanticCacheHit, ResultCache (protocols)
├── session.py       Session, RunId
├── messages.py      FoundryMessage, MessageRole, ContentBlock
├── model.py         ModelResponse, ModelDelta, StopReason
├── events.py        RunEvent tagged union, InboundMessage tagged union
├── state.py         StateBase, Reducer, ReducerType
├── errors.py        exception hierarchy
└── types.py         shared Pydantic primitives (RunId, ArtifactRef stubs, etc.)
```

### What `core` imports

Only: `pydantic`, `anyio`, stdlib. That's it. No `langgraph`, no `langchain_*`, no `anthropic`, no `openai`, no other foundry module.

### What imports `core`

Every other foundry module. `core` is the bottom of the dependency graph — see the module dependency diagram in `01-architecture-overview.md`.

### Enforcement

`ruff.toml` enforces via `flake8-tidy-imports`-style rules:

```toml
[tool.ruff.lint.flake8-tidy-imports.banned-api]
# in core/*
"langgraph".msg = "core must not import langgraph; use foundry.runtime adapter"
"langchain_core".msg = "core must not import langchain; use foundry.runtime adapter"
"anthropic".msg = "core must not import provider SDKs; use foundry.providers"
"openai".msg = "core must not import provider SDKs; use foundry.providers"
"foundry.providers".msg = "core must not import providers (circular layer)"
"foundry.runtime".msg = "core must not import runtime adapter (circular layer)"
"foundry.config".msg = "core must not import config (circular layer)"
```

A CI check runs `ruff check src/foundry/core/` with a gate on zero violations.

## Tour of the key types

| Type | File | One-liner |
|---|---|---|
| `Agent` | `agent.py` | Protocol: an LLM-driven node — produces a response given state + session via prompt + tools. |
| `BaseAgent` | `agent.py` | Convenience base class implementing `Agent` with lifecycle-hook plumbing. |
| `FunctionNode` | `function_node.py` | Protocol: a deterministic Python node — runs a function over state, no LLM. Same flow position as an agent. |
| `BaseFunctionNode` | `function_node.py` | Convenience base class implementing `FunctionNode` with lifecycle-hook plumbing + observability. |
| `Node` | `node.py` | Common protocol both `Agent` and `FunctionNode` satisfy: anything the orchestration compiler accepts as a flow node. |
| `LifecycleHooks` | `agent.py` | Optional pre/post/error hooks users can attach to an agent, function node, or session. |
| `Tool` | `tool.py` | Protocol: a callable with typed input/output schemas and an async handler. |
| `BaseTool` | `tool.py` | Convenience base class for authoring tools in Python. |
| `ToolRegistry` | `tool.py` | Name-and-version indexed lookup; enforces agent-level allowlists. |
| `RunContext` | `tool.py` | Handle threaded into tool handlers; exposes session + logger + cancellation + connection accessor. |
| `Connection` | `connection.py` | Protocol: an authenticated, pooled handle to an external system (Snowflake, Slack, etc.). |
| `ConnectionPool` | `connection.py` | Protocol: per-process pool that issues, caches, refreshes, and closes connections. |
| `ConnectionAccessor` | `connection.py` | Interface threaded into `RunContext`; tool handlers call `ctx.connections.get(slot)`. |
| `ConnectionFactory` | `connection.py` | Protocol: what `catalog/connections/<name>/v<N>/auth.py` modules export; builds a Connection from typed config + resolved credentials. |
| `ConnectionHealth` | `connection.py` | Typed result of a connection health check. |
| `Embedder` | `embedder.py` | Protocol: produces vector embeddings from text. Separate from `Provider` (generation) because vendors and models differ. |
| `Embedding` | `embedder.py` | Typed result of an embedding call: vector, dimensions, model, tokens. |
| `EmbedderCapabilities` | `embedder.py` | Static descriptor: provider, model, dimensions, max input tokens, query/document split support, pricing. |
| `Retriever` | `retrieval.py` | Protocol: returns relevant documents for a query. Implementations: dense, sparse (BM25), hybrid. |
| `Reranker` | `retrieval.py` | Protocol: rescores a candidate list of documents. Separate from `Embedder` because it uses cross-encoder models. |
| `RetrievedDocument` | `retrieval.py` | Pydantic: id, text, score, source, metadata. |
| `Memory` | `memory.py` | Protocol: coordinates multi-layer memory — read, write, consolidate. |
| `MemoryLayer` | `memory.py` | Protocol: a single memory layer (working / episodic / semantic / custom). |
| `MemoryEnvelope` | `memory.py` | Pydantic: per-layer contributions assembled for prompt injection. |
| `MemoryContribution` | `memory.py` | Pydantic: one layer's typed output (messages list / docs / synthesised text). |
| `MemoryWrite` | `memory.py` | Pydantic: a write payload, routed by kind to the appropriate layer. |
| `SemanticCache` | `cache.py` | Protocol: similarity-based cache for LLM responses. Optional; opt-in per agent. |
| `SemanticCacheKey` | `cache.py` | Pydantic: structural hash + embedding vector of the inputs being cached. |
| `SemanticCacheHit` | `cache.py` | Pydantic: result of a lookup hit — cached response + similarity score + metadata. |
| `ResultCache` | `cache.py` | Protocol: exact-match cache for tool results (keyed by hash of validated input). |
| `Session` | `session.py` | Immutable bundle of `run_id` + trace + logger + checkpointer + cost-budget handles. |
| `CostBudget` | `session.py` | Per-run dollar cap. Provider layer checks pre-call, records actual cost post-call; `CostBudgetExceeded` raised when next call would breach. |
| `RunId` | `session.py` / `types.py` | ULID-based run identifier; string-serialisable. |
| `FoundryMessage` | `messages.py` | Provider-agnostic message: role + content blocks. |
| `MessageRole` | `messages.py` | Enum: `system`, `user`, `assistant`, `tool`. |
| `ContentBlock` | `messages.py` | Tagged union: `TextBlock`, `ToolUseBlock`, `ToolResultBlock`, `ImageBlock`. |
| `ModelResponse` | `model.py` | Result of a non-streaming LLM call. |
| `ModelDelta` | `model.py` | A chunk of a streaming LLM call. |
| `StopReason` | `model.py` | Enum: `end_turn`, `max_tokens`, `stop_sequence`, `tool_use`, `error`, `filtered`. |
| `RunEvent` | `events.py` | Tagged union: progressive events a run emits (`run.started`, `llm.delta`, `tool.started`, …). The spine of every streaming surface. |
| `InboundMessage` | `events.py` | Tagged union: messages a client can send *into* a streaming run over WebSocket (`InjectInput`, `ApprovalResponse`, `Cancel`, `Pause`, `Resume`). |
| `StateBase` | `state.py` | Base class for state schemas; collects reducer metadata. |
| `Reducer` | `state.py` | Enum: `APPEND`, `MERGE`, `LAST_WRITE_WINS`, `REPLACE_IF_SET`. |
| `FoundryError` et al. | `errors.py` | Hierarchy — see § Exception hierarchy. |

## The `Agent` protocol

```python
from typing import Protocol, runtime_checkable

@runtime_checkable
class Agent(Protocol):
    """
    The foundry's root agent abstraction.

    Implementations are responsible for producing a state delta and/or a final
    output given the current state and a session. An Agent is async and
    stateless across runs — per-run state lives in the session and the State
    object it mutates. Agents do not hold long-lived resources.
    """

    name: str
    """Stable human-readable identifier. Unique within a project."""

    version: str
    """Semver-ish version of the agent. For compiled agents this is the
    `agent.yaml` content hash; for the meta-agent this is a coded version."""

    async def run(
        self,
        state: StateBase,
        session: Session,
    ) -> AgentResult: ...

class AgentResult(BaseModel):
    """Typed result of a single agent step."""
    state_delta: dict[str, Any]
    """Fields to write back to state. Reducer semantics apply."""

    output: Any | None = None
    """Final output when this agent is the run's terminal producer.
    None when this is a supervisor / intermediate step."""

    next: str | Literal["END"] | None = None
    """Explicit next-hop hint. Orchestration compiler may honour or ignore
    based on the pattern. `None` means 'let the pattern decide'."""
```

### Why a protocol, not an ABC

The foundry has multiple agent producers:
- Compiled agents from YAML (`foundry.orchestration.compiler` produces these).
- The meta-agent (`foundry.configurator.MetaAgent` — hand-written).
- Test fakes and agents authored ad-hoc in notebooks.

A protocol lets all three share the contract without inheritance coupling. `@runtime_checkable` enables `isinstance(x, Agent)` for defensive checks at boundary points.

### `BaseAgent`

A convenience class that implements `Agent` and provides:
- `LifecycleHooks` wiring — pre/post/error hooks fire around `run()`.
- Automatic `run_id`-scoped structured logging with agent name/version.
- OTel span (`foundry.node`) entered on `run()` start, closed on return.
- Cancellation surface: if the session is cancelled, `run()` raises `CancelledError` at the next `await`.

```python
class BaseAgent(Agent):
    def __init__(
        self,
        name: str,
        version: str,
        hooks: LifecycleHooks | None = None,
    ) -> None:
        self.name = name
        self.version = version
        self._hooks = hooks or LifecycleHooks()

    async def run(self, state: StateBase, session: Session) -> AgentResult:
        async with session.span("foundry.node", agent=self.name, version=self.version):
            await self._hooks.before_node(self, state, session)
            try:
                result = await self._step(state, session)
            except Exception as exc:
                await self._hooks.on_error(self, exc, session)
                raise
            await self._hooks.after_node(self, result, state, session)
            return result

    async def _step(self, state: StateBase, session: Session) -> AgentResult:
        raise NotImplementedError
```

Subclasses override `_step`. All the instrumentation and lifecycle plumbing is handled by the base class.

### `LifecycleHooks`

```python
class LifecycleHooks(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    before_run: Callable[[Session], Awaitable[None]] | None = None
    after_run: Callable[[Session, AgentResult | None], Awaitable[None]] | None = None
    before_node: Callable[[Agent, StateBase, Session], Awaitable[None]] | None = None
    after_node: Callable[[Agent, AgentResult, StateBase, Session], Awaitable[None]] | None = None
    on_error: Callable[[Agent, Exception, Session], Awaitable[None]] | None = None
    before_tool: Callable[[Tool, Any, Session], Awaitable[None]] | None = None
    after_tool: Callable[[Tool, Any, Any, Session], Awaitable[None]] | None = None
```

Hooks are **optional** and **never swallow exceptions** — they log and re-raise. They are not a dependency-injection mechanism; they are an instrumentation mechanism. Use them for: cross-cutting metrics, audit-trail-augmentation, circuit-breaker wiring. Do not use them for: business logic, state mutation, control-flow alteration.

Methods on the base class are no-ops when the corresponding hook is `None`, so there is no cost to leaving them unset.

## The `FunctionNode` protocol

Some flow nodes don't need an LLM: input normalisation, output formatting, deterministic routing, business-rule gates, pure data transformations. Forcing them through `Agent` (with a stub LLM call) wastes cost + latency and pollutes the audit trail with vacuous LLM events.

`FunctionNode` is the deterministic-Python alternative. Same flow position as `Agent` — both implement `Node` — same state-visibility enforcement, same retry/timeout/observability/checkpointing. Just no LLM, no tools, no prompt, no output schema (the function returns a state delta directly).

```python
@runtime_checkable
class FunctionNode(Protocol):
    name: str
    version: str
    """Content hash of the function source + node config — same role as
    Agent.version. Caches and audit identity key on it."""

    async def run(
        self,
        state: StateBase,
        session: Session,
    ) -> NodeResult: ...

class NodeResult(BaseModel):
    """Common result shape for both Agent and FunctionNode."""
    state_delta: dict[str, Any]
    output: Any | None = None      # populated by Agent on terminal turn; usually None for FunctionNode
    next: str | Literal["END"] | None = None
```

`Agent.run()` returns `AgentResult` which is a `NodeResult` subclass with the LLM-specific output extraction. `FunctionNode.run()` returns plain `NodeResult` with `state_delta` populated and `output` typically None.

### `BaseFunctionNode`

```python
class BaseFunctionNode(FunctionNode):
    def __init__(
        self,
        name: str,
        version: str,
        hooks: LifecycleHooks | None = None,
        retry_policy: RetryPolicy | None = None,
        timeout_s: float = 30.0,
    ) -> None: ...

    async def run(self, state: StateBase, session: Session) -> NodeResult:
        async with session.span("foundry.function_node", node=self.name, version=self.version):
            await self._hooks.before_node(self, state, session)
            try:
                result = await self._step(state, session)
            except Exception as exc:
                await self._hooks.on_error(self, exc, session)
                raise
            await self._hooks.after_node(self, result, state, session)
            return result

    async def _step(self, state: StateBase, session: Session) -> NodeResult:
        raise NotImplementedError
```

Concrete function nodes are loaded by the compiler from a project's `functions/<name>/function.py`; the compiler wraps `function.run` in a `BaseFunctionNode` subclass that delegates to it. Detail in `21-agent-system.md` § Function nodes.

### Why `FunctionNode` is a separate protocol (not just `Agent` with `model_binding=None`)

The schemas diverge:
- `Agent` has `model_binding`, `prompt`, `output_schema`, `tools`, `iteration_limit`, `semantic_cache`, `retrievers`, `memory` — none of which apply to a function node.
- `FunctionNode` has just `function_ref`, `state_visibility`, `retry_policy`, `timeout_s`.

Forcing both into one Pydantic schema with optional fields produces a schema where half the fields are mutually exclusive based on a discriminator. Cleaner to split. The shared abstraction lives at the `Node` protocol level (which the orchestration compiler programs against).

### `Node` protocol (shared parent)

```python
@runtime_checkable
class Node(Protocol):
    name: str
    version: str
    async def run(self, state: StateBase, session: Session) -> NodeResult: ...
```

Anything implementing `Node` is a valid flow node. Both `Agent` and `FunctionNode` satisfy it by virtue of having the right methods (Protocol is structural). The compiler accepts a `dict[str, Node]` registry and doesn't care which kind each entry is.

### When to use which

| Use case | Pick |
|---|---|
| Decision needs LLM judgement (classification, summarisation, free-text generation) | `Agent` |
| Tool calls with branching reasoning between them | `Agent` (loop with iteration_limit) |
| Deterministic input shape transformation | `FunctionNode` |
| Final output formatting / aggregation | `FunctionNode` |
| Business-rule gate ("if amount < $X, skip downstream") | `FunctionNode` |
| Deterministic routing (when conditions are simple expressions over state) | Use a `graph` flow's `when:` predicate, not even a function node |
| Side effects (write to a DB, send an alert) | A `Tool` called by an Agent — tools are how the foundry interacts with side-effecting systems |

`FunctionNode` does NOT replace tools. Tools are how you interact with the outside world (with connections, retries, sandboxing). Function nodes are how you compute over state without an LLM.

## The `Tool` protocol

Full detail in `20-tool-system.md`. The core contract, for reference:

```python
@runtime_checkable
class Tool(Protocol):
    name: str
    version: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]

    async def handle(
        self,
        inputs: BaseModel,
        ctx: RunContext,
    ) -> BaseModel: ...
```

`input_schema` and `output_schema` are the Pydantic models used for validation at the boundary. `handle` receives an already-validated inputs instance and MUST return an instance of `output_schema` or raise a `ToolError`.

## `RunContext`

The handle threaded into tool handlers:

```python
class RunContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    run_id: RunId
    agent_name: str
    """The agent whose turn invoked this tool."""

    session: Session
    """Full session handle — logger, tracer, cancellation token, checkpointer."""

    tool_ref: str
    """Canonical ref of the tool being invoked, e.g. 'catalog/query_snowflake@v2'."""

    timeout_s: float | None
    """Per-tool timeout from agent config; None = agent-level default."""

    retry_policy: RetryPolicy
    """Resolved retry policy (attempts, backoff, retryable errors)."""

    connections: ConnectionAccessor
    """Slot-name → Connection accessor. Tool handlers request pooled,
    authenticated external-system connections via ctx.connections.get(slot).
    Only slots the tool declared in `connections_required` are accessible;
    accessing an unbound slot raises ConnectionSlotNotBoundError."""
```

`RunContext` is constructed by the orchestration runtime (inside the LangGraph adapter) and passed positionally to tool handlers. Handlers MUST NOT store it beyond the call — it is valid for the duration of a single `handle` invocation only. Connections acquired via `ctx.connections.get(...)` are owned by the pool, not the handler; handlers do not close them.

## Connections

The `Connection` primitive is the standardized, versioned, pooled handle to an external enterprise system (Snowflake, Postgres, Slack, S3, Salesforce, etc.). Tools declare connection *slots* they need; projects *bind* slots to specific versioned connections in `system.yaml`; the runtime issues live authenticated clients via a pool. Full spec in `23-connections-and-auth.md`.

This section defines only the core types and contracts — enough for every module above to program against.

### The `Connection` protocol

```python
T = TypeVar("T")

@runtime_checkable
class Connection(Protocol, Generic[T]):
    """An authenticated handle to one external system.

    Built by a ConnectionFactory and cached by the ConnectionPool.
    Tool handlers receive already-constructed Connections via RunContext.
    """
    ref: str
    """Canonical ref: 'catalog/snowflake@v1' or 'local/custom_system@v2'."""

    slot: str
    """Slot name the tool requested: e.g. 'warehouse'. Distinct from `ref` —
    a tool asks for the slot, the project resolves which ref fills it."""

    @property
    def client(self) -> T:
        """The underlying authenticated client object. Its type is
        connection-specific (snowflake.connector.SnowflakeConnection,
        slack_sdk.WebClient, asyncpg.Pool, httpx.AsyncClient, etc.).
        Type is carried at the call site via the TypeVar."""

    async def health(self) -> ConnectionHealth: ...
```

Connections are NOT `BaseModel`s — the underlying client objects rarely serialise and we don't want to fake it. They're plain protocol types. Serialisation for observability is via `ConnectionDescriptor` (see below).

### `ConnectionHealth`

```python
class ConnectionHealth(BaseModel):
    ok: bool
    latency_ms: int | None = None
    message: str = ""
    checked_at: datetime
```

### `ConnectionAccessor`

The surface tool handlers see:

```python
class ConnectionAccessor(Protocol):
    async def get(self, slot: str) -> Connection:
        """Return the connection bound to the given slot for this tool call.

        Raises:
            ConnectionSlotNotDeclaredError: the tool did not declare this slot
                in its ToolSpec.connections_required. Compile-time catch;
                this runtime raise is a defence-in-depth.
            ConnectionSlotNotBoundError: the project's system.yaml did not
                bind this slot to a connection. Compile-time catch normally;
                runtime raise on dynamic-config edge cases.
            ConnectionAuthError: auth failed when building the connection.
            ConnectionTimeoutError: connection construction exceeded budget.
        """

    async def health(self, slot: str) -> ConnectionHealth: ...

    def descriptor(self, slot: str) -> ConnectionDescriptor:
        """Non-authenticating metadata describing the connection bound to
        this slot. Safe to include in logs / traces / error messages."""
```

### `ConnectionDescriptor`

Serialisable, secret-free summary of a connection, emitted on every `foundry.connection` observability event and included in tool-call span attributes:

```python
class ConnectionDescriptor(BaseModel):
    ref: str                        # 'catalog/snowflake@v1'
    slot: str                       # 'warehouse'
    auth_scheme: AuthScheme
    config_hash: str                # short hex hash of the resolved config (minus secrets)
    principal: str | None = None    # who we authed as (service-account email, IAM role arn, etc.)
    redacted_config: dict[str, Any] = Field(default_factory=dict)
```

`redacted_config` contains only fields marked non-sensitive in the connection's schema. The redactor is conservative — anything not explicitly marked `is_sensitive=False` is dropped.

### `ConnectionFactory`

What `catalog/connections/<name>/v<N>/auth.py` modules export. The pool calls the factory to build a Connection from typed config + resolved credentials:

```python
class ConnectionFactory(Protocol):
    async def __call__(
        self,
        config: BaseModel,              # instance of the connection's declared config schema
        credentials: ResolvedCredentials,
        ctx: ConnectionContext,         # {pool_logger, tracer, cancel_token}
    ) -> Connection: ...
```

Full auth-scheme catalogue (api_key, oauth2_client_credentials, oauth2_refresh_token, jwt_bearer, sigv4, mtls, basic_auth, custom) is in `23-connections-and-auth.md` — each scheme is a helper the factory composes with.

### `ConnectionPool`

```python
class ConnectionPool(Protocol):
    """Per-process singleton. Holds connection instances keyed by
    (ref, config_hash, project). Handles: caching, concurrency limits,
    token refresh, graceful close."""

    async def acquire(
        self,
        ref: str,
        config_hash: str,
        project: str,
        factory: ConnectionFactory,
        factory_args: FactoryArgs,
    ) -> Connection:
        """Return an existing connection if one exists for
        (ref, config_hash, project); otherwise build one via factory.
        Concurrent acquires for the same key coalesce on a single build."""

    async def release(self, conn: Connection) -> None:
        """Return a connection to the pool. For long-lived clients this is
        typically a no-op; for per-request clients it closes."""

    async def refresh(self, ref: str, project: str) -> None:
        """Force-refresh (token rotation, cert reload, etc.). Evicts the
        current entry; next acquire rebuilds."""

    async def evict(self, ref: str, project: str | None = None) -> None: ...
    async def close_all(self) -> None: ...
```

A pool entry's lifecycle:

```
  first acquire  ──▶  factory builds Connection  ──▶  cached
                                                        │
                                                        ├─▶ subsequent acquires return cached
                                                        │
              refresh (scheduled or on-auth-error) ─────┤
                                                        │
                                                        ▼
                                                  evict + rebuild on next acquire
                                                        │
                                     process shutdown ──┴─▶ close_all awaits all .close()
```

### Cancellation

Every `acquire`, `health`, and `close` respects `session.cancel_token`. A cancelled acquire during a long OAuth flow unwinds cleanly — no orphan HTTP calls, no half-opened DB connections.

### Why connections live in core (not providers)

They are a cross-cutting primitive — the eval harness, the meta-agent's tool scaffolder, the API layer, and the observability layer all need to know about them. Putting the protocol in `core` keeps `foundry.core` as the single source of truth for primitives that cross layer boundaries, and avoids a circular dependency where, say, `providers/` would need `connections/` and vice versa.

The *concrete* auth-scheme helpers and pool implementation live in `foundry.auth` and `foundry.connections` respectively — see module layout in `01-architecture-overview.md`.

## Embeddings

Embedding calls are a distinct modality from generation calls — different vendors specialise (Voyage, Cohere, OpenAI's `text-embedding-*`), different pricing curves, different capabilities. The foundry treats them as first-class via a separate `Embedder` protocol. Semantic caching + RAG workflows depend on this abstraction.

### `Embedder` protocol

```python
@runtime_checkable
class Embedder(Protocol):
    name: str
    """Canonical provider name, e.g. 'voyage', 'openai', 'cohere'."""

    model: str
    """Model id the embedder is bound to, e.g. 'voyage-3', 'text-embedding-3-small'."""

    capabilities: EmbedderCapabilities

    async def embed(
        self,
        inputs: list[str],
        purpose: Literal["query", "document"] = "document",
    ) -> list[Embedding]:
        """Embed a batch of texts.

        `purpose` distinguishes retrieval-query vs retrieval-document embeddings
        for vendors that support asymmetric embedding (Voyage, Cohere v3+).
        Vendors without the distinction ignore the arg.

        Raises:
            EmbedderConfigError: unsupported model / invalid input size.
            EmbedderAuthError: credentials rejected.
            EmbedderError: anything else from the embedder vendor.
        """
```

### `Embedding`

```python
class Embedding(BaseModel):
    model_config = ConfigDict(frozen=True)
    vector: list[float]
    dimensions: int
    model: str           # echoed from the embedder; makes stored vectors self-describing
    input_tokens: int
    latency_ms: int
    cost_estimate_usd: Decimal | None = None
```

### `EmbedderCapabilities`

```python
class EmbedderCapabilities(BaseModel):
    provider: str
    model: str
    dimensions: int
    max_input_tokens: int
    supports_query_document_split: bool = False
    supports_batch: bool = True
    max_batch_size: int = 128
    pricing: EmbedderPricing           # $/1M input tokens

    def dim_matches(self, other: "EmbedderCapabilities") -> bool:
        return self.dimensions == other.dimensions
```

### Why separate from `Provider`

- Embedders and chat models are authenticated through different endpoints even for the same vendor (OpenAI Embeddings API vs Chat Completions API). Mixing them in one protocol forces every provider adapter to care about both.
- Anthropic does not ship their own embedding model in 2026 — they recommend Voyage. The foundry's primary LLM provider and embedder will often be *different vendors*.
- Semantic caching and RAG use embedders without ever calling `generate()` — keeping them separate lets these subsystems depend only on what they need.

Full per-provider embedder detail and concrete implementations in `11-provider-abstraction.md`.

## Retrieval primitives

RAG pipelines need at minimum a retriever; production pipelines typically layer a reranker and often combine dense + sparse retrieval. These are first-class primitives so that agents can consume them uniformly, catalog templates can ship them, and observability can trace them.

### `Retriever` protocol

```python
@runtime_checkable
class Retriever(Protocol):
    name: str
    kind: Literal["dense", "sparse", "hybrid"]
    """For observability and reasoning about behaviour. Dense = embedding
    similarity. Sparse = lexical (BM25 / vendor-sparse). Hybrid = both
    combined via RRF or weighted merge."""

    async def retrieve(
        self,
        query: str,
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedDocument]:
        """Return ranked documents. Ordering is the retriever's
        responsibility; downstream rerankers can reorder. Filters are
        interpreted by the retriever (e.g., {source: 'docs', year: 2026}
        translates to a metadata filter against the backing store)."""
```

### `Reranker` protocol

```python
@runtime_checkable
class Reranker(Protocol):
    name: str
    model: str

    async def rerank(
        self,
        query: str,
        documents: list[RetrievedDocument],
        top_k: int | None = None,
    ) -> list[RetrievedDocument]:
        """Rescore and reorder. Truncates to top_k if provided.
        Uses cross-encoder models (Cohere Rerank, Voyage Rerank, Jina, etc.)
        which score (query, doc) pairs directly — more accurate than
        embedding similarity, too expensive for initial retrieval."""
```

### `RetrievedDocument`

```python
class RetrievedDocument(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: str
    text: str
    score: float
    """Retriever-specific score (cosine similarity, BM25 score, RRF rank, etc.).
    Normalise comparisons across retrievers via rank-based metrics."""

    source: str | None = None
    """Where the doc came from — a collection name, a url, etc."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    """Arbitrary attached data: author, timestamp, chunk_index, etc."""
```

### Composition

Retrievers and rerankers compose as ordinary async functions. A typical production pipeline:

```python
# Agent-side or tool-side code:
docs = await retriever.retrieve(query, top_k=50)
docs = await reranker.rerank(query, docs, top_k=8)
# docs now ready to inject into the prompt
```

The orchestration layer emits `foundry.retrieval` events (see below) for every retriever and reranker call so the audit trail is complete.

### Why separate from `Tool`

Retrievers *can* be wrapped as tools (the LLM calls `retrieve_documents(query)`). They can also be invoked *before* the agent starts, as part of building the initial prompt — the LLM never decides whether to retrieve. Both patterns are legitimate; the protocol supports both.

Full spec (implementations, hybrid-fusion strategies, backend catalog entries, RAG patterns, failure modes) in `25-retrieval-and-rag.md`.

## Memory primitives

The foundry treats agent memory as a **composition over existing primitives** rather than a new heavy subsystem. A `Memory` is a coordinator that gathers contributions from one or more `MemoryLayer`s, assembles them into a typed envelope for prompt injection, and routes writes to the appropriate layer. Three standard layer kinds are shipped — working (recency window), episodic (retrieval-based), and semantic (synthesised) — each implemented on top of primitives we already have (state + reducer, `Retriever`, state + lifecycle hook).

Cross-session persistent memory (a `MemoryStore` keyed on user / session) is **explicitly deferred** to v1.1; the spec documents the gap and where it would slot in without changing today's protocols.

Full behavioural spec (lifecycle, prompt-assembly rules, consolidation, failure modes, observability, deferred work) in `26-memory-and-context.md`. Core types below.

### `Memory` protocol

```python
@runtime_checkable
class Memory(Protocol):
    layers: list[MemoryLayer]

    async def read(
        self,
        query: str,
        ctx: MemoryContext,
    ) -> MemoryEnvelope:
        """Gather contributions from each configured layer in declared order.
        Layer-level errors degrade gracefully: a failed layer contributes
        an empty contribution + warning event, rather than aborting the read."""

    async def write(
        self,
        content: MemoryWrite,
        ctx: MemoryContext,
    ) -> None:
        """Route to the appropriate layer. If `content.target_layer` is set
        the routing is explicit; otherwise the kind selects a default
        (`message` → working, `summary`/`fact` → semantic, `raw` → all
        writable layers)."""

    async def consolidate(self, ctx: MemoryContext) -> None:
        """Trigger consolidation across layers (e.g., summarise older
        working messages into the semantic layer). Called by lifecycle
        hooks at configured triggers (every N turns, end of session,
        explicit invocation)."""
```

### `MemoryLayer` protocol

```python
@runtime_checkable
class MemoryLayer(Protocol):
    kind: Literal["working", "episodic", "semantic", "custom"]
    name: str
    """Stable identifier within an agent. Referenced from
    MemoryInjectionRule and from MemoryWrite.target_layer."""

    async def read(
        self,
        query: str,
        ctx: MemoryContext,
    ) -> MemoryContribution: ...

    async def write(
        self,
        content: MemoryWrite,
        ctx: MemoryContext,
    ) -> None:
        """No-op for layers that don't accept writes (some episodic
        configurations are read-only against an external corpus)."""

    async def consolidate(self, ctx: MemoryContext) -> None:
        """No-op for stateless layers (working from state, episodic
        from external store). Meaningful for semantic — typically runs
        an LLM call to summarise recent activity into the layer's
        backing field."""
```

### Types

```python
class MemoryContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    run_id: RunId
    agent_name: str
    session: Session

    state_view: dict[str, Any]
    """Read-only projection of the current state, scoped to fields
    the memory subsystem may read. Provided by the orchestration runtime
    on every memory call."""

    state_writer: Callable[[str, Any], Awaitable[None]] | None = None
    """Bound callable that writes a state field within the agent's
    declared write scope. None when the memory layer is read-only in
    this context."""

class MemoryContribution(BaseModel):
    model_config = ConfigDict(frozen=True)
    layer_name: str
    layer_kind: Literal["working", "episodic", "semantic", "custom"]
    content: list[FoundryMessage] | list[RetrievedDocument] | str
    """Working returns a message list, episodic returns retrieved docs,
    semantic returns synthesised text. Custom layers pick one of the
    three carriers."""
    tokens_estimate: int
    metadata: dict[str, Any] = Field(default_factory=dict)

class MemoryEnvelope(BaseModel):
    contributions: list[MemoryContribution]
    """Ordered as declared in MemoryConfig.layers."""
    total_tokens_estimate: int
    truncated: bool = False
    """True if any layer's contribution was truncated due to per-rule
    or envelope-level token limits."""

class MemoryWrite(BaseModel):
    kind: Literal["message", "summary", "fact", "raw"]
    content: str | FoundryMessage
    target_layer: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
```

### Why a coordinator rather than direct state munging

- **Uniformity across projects.** Every project that needs memory configures it the same way; meta-agent can scaffold `memory:` blocks without each project reinventing the conventions.
- **Eval-driven tuning.** Memory becomes an explicit configuration axis; `compare_versions` can vary it (turn off episodic, change window size, swap semantic consolidator) and measure end-to-end impact.
- **Observability.** Memory reads/writes/consolidations are typed events in the run stream — debugging long conversations becomes "show me the memory envelope this agent saw."
- **Fail-safe composition.** A failing episodic retriever degrades to "no episodic context this turn" rather than blowing up the whole agent step. Coordinator owns the degrade-vs-fail policy.

### Why not a heavy persistent store in v1

The three standard layers cover ~90% of what conversational agents actually use. All three implement on top of *existing* primitives:

- Working = state field + reducer.
- Episodic = `Retriever` over a conversation-history corpus.
- Semantic = state field + consolidator hook.

A persistent cross-session `MemoryStore` (e.g. user-keyed memory across visits) is real but heavier — a backing DB, retention rules, identity management. That's a v1.1 concern when a real customer-facing project surfaces. The current protocol shape doesn't preclude it; a future `PersistentSemanticLayer` reads/writes a cross-session store using the same `MemoryLayer` interface.

## Caching primitives

The foundry supports three distinct cache layers, each at a different level:

| Layer | What's cached | Keyed by | Where configured |
|---|---|---|---|
| **Prompt caching** (provider-native) | Prompt prefix reuse across calls | Exact bytes of cacheable blocks | `ModelSettings.cache_control` + `TextBlock.cache_control` |
| **Semantic caching** | Whole `ModelResponse` for a given agent call | Structural hash + embedding similarity of input messages + tools | Per-agent `SemanticCacheConfig` (opt-in) |
| **Tool-result caching** | Tool output for idempotent tools | Exact hash of validated input | Per-tool `cacheable: bool` + `cache_ttl_s` |

All three compose. Prompt caching reduces per-call cost when semantic cache misses; semantic cache short-circuits the call entirely on similarity hit; tool-result cache short-circuits tool calls on exact-match hit.

Full behavioural spec, correctness rules, and configuration schemas in `24-caching-and-optimisation.md`. The core protocols are below.

### `SemanticCache` protocol

```python
class SemanticCache(Protocol):
    """Similarity-based cache for full ModelResponses.

    Opt-in per agent. Correctness hazard if thresholds are loose —
    an LLM response cached at similarity 0.92 may not be a correct
    response to a 0.92-similar but materially different input.
    Threshold discipline is the operator's responsibility."""

    async def lookup(
        self,
        key: SemanticCacheKey,
        threshold: float,
    ) -> SemanticCacheHit | None: ...

    async def store(
        self,
        key: SemanticCacheKey,
        response: ModelResponse,
        ttl_s: int,
    ) -> None: ...

    async def invalidate(self, agent_name: str) -> None:
        """Evict all cached entries for an agent. Called on agent-version
        change (prompt or tool-binding edit) to prevent stale hits."""
```

### `SemanticCacheKey`

```python
class SemanticCacheKey(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_name: str
    agent_version: str
    """Content-hash of the agent config at call time. Changes invalidate
    all cached entries for that agent — see SemanticCache.invalidate()."""

    model_binding_hash: str
    """Hash of (provider + model + temperature + max_tokens + response_format).
    Different model settings get different cache entries."""

    tools_hash: str
    """Exact hash of the tool schemas presented to the LLM. Different
    tool sets get different cache entries."""

    messages_structural_hash: str
    """Hash of messages with content text stripped. Catches structural
    changes (e.g. new tool-result blocks) independent of semantic content."""

    messages_embedding: Embedding
    """Vector used for similarity search. Embedded from the
    concatenated textual content of messages."""
```

Lookup semantics: structural hash + model binding + tools must match exactly; semantic similarity is computed over the embedding within that exact-match bucket. This prevents cross-bucket false hits (an agent with tool-set A never hits an entry cached from the same messages under tool-set B).

### `SemanticCacheHit`

```python
class SemanticCacheHit(BaseModel):
    response: ModelResponse
    similarity: float
    cached_at: datetime
    original_input_preview: str | None = None   # truncated, for debugging
```

### `ResultCache` (tool-result exact-match)

```python
class ResultCache(Protocol):
    """Exact-match cache for tool outputs. Per-tool opt-in via
    ToolSpec.cacheable; keyed by hash of validated input. Safe by
    default because it only caches tools the author explicitly marked
    as idempotent."""

    async def lookup(
        self,
        tool_ref: str,
        tool_version: str,
        input_hash: str,
    ) -> BaseModel | None: ...

    async def store(
        self,
        tool_ref: str,
        tool_version: str,
        input_hash: str,
        output: BaseModel,
        ttl_s: int,
    ) -> None: ...
```

Both cache protocols are accessed by upper layers via `Session` — the session carries a `cache: CacheAccessor` bundle with `semantic`, `tool_result`, and a no-op fallback when caching isn't configured. Tool handlers and provider calls never construct caches directly.

### `CacheAccessor` on `Session`

`Session` gains one field:

```python
cache: CacheAccessor
"""Session-scoped accessor. .semantic, .tool_result, or NoOp when
caching is disabled. Upper layers don't special-case configuration —
they call through the accessor, and absent backends mean miss."""
```

No other core types change for caching.

## `Session`

```python
class Session(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    run_id: RunId
    project: str
    system_version: str        # git sha
    pin_set_hash: str          # hash of resolved version pins
    started_at: datetime

    logger: BoundLogger        # structlog-style BoundLogger bound to run_id
    tracer: Tracer             # OTel tracer; convenience .span() below
    cancel_token: CancelToken  # propagates cancellation
    checkpointer: CheckpointerHandle  # opaque handle to the adapter's checkpointer
    cost_budget: CostBudget | None     # per-run dollar cap; None = unbounded

    async def span(self, name: str, **attrs: Any) -> AbstractAsyncContextManager[Span]: ...
```

### `CostBudget`

Hard per-run dollar cap that the provider layer enforces around every LLM call (and cooperatively around every Reranker / Embedder call). Constructed once at run start from `Guardrails.max_cost_usd`; mutated by the provider adapter as calls succeed.

```python
class CostBudget(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    """Mutable internal state — same pattern as CancelToken. Session
    itself remains frozen; the budget object inside it tracks accumulated
    spend across calls."""

    max_usd: Decimal
    accumulated_usd: Decimal = Decimal("0")

    def remaining_usd(self) -> Decimal:
        return self.max_usd - self.accumulated_usd

    def check(self, estimated_usd: Decimal) -> None:
        """Raise CostBudgetExceeded if the next call would breach budget.
        Provider adapters call this BEFORE the LLM call using the
        provider's cost estimator (input_tokens × input_price + buffer
        for output)."""
        if self.accumulated_usd + estimated_usd > self.max_usd:
            raise CostBudgetExceeded(
                f"call would push spend to ${self.accumulated_usd + estimated_usd}, "
                f"budget ${self.max_usd}",
                context={
                    "max_usd": str(self.max_usd),
                    "accumulated_usd": str(self.accumulated_usd),
                    "estimated_usd": str(estimated_usd),
                    "remaining_usd": str(self.remaining_usd()),
                },
            )

    def record(self, actual_usd: Decimal) -> None:
        """Update accumulated spend after a call completes successfully.
        Called by the provider adapter once the response is received."""
        self.accumulated_usd += actual_usd
```

Composition: `Guardrails.max_cost_usd: Decimal | None` (in `SystemSpec`) → `CostBudget(max_usd=...)` constructed at session start → bound to `Session.cost_budget` → checked + recorded by every `ProviderAdapter` call. `None` for `max_cost_usd` skips construction entirely (no overhead).

`CostBudgetExceeded` lives under `OrchestrationError` because it halts the run cleanly — same shape as `MaxHopsExceededError`.

### `RunId`

ULID (26-char Crockford-base32 string). Sortable by time prefix, unique, URL-safe.

```python
class RunId(str):
    """Typed ULID string. Use RunId.new() to mint."""
    @classmethod
    def new(cls) -> "RunId": ...
    @classmethod
    def validate(cls, v: str) -> "RunId": ...
```

Immutability: `Session` is frozen. Anything that wants a modified session constructs a new one (e.g., child span with a different logger binding). This keeps the abstraction composable and safe to pass around freely.

### `CancelToken`

Wraps `anyio.CancelScope` to present a minimal interface:

```python
class CancelToken:
    def cancelled(self) -> bool: ...
    async def wait_cancelled(self) -> None: ...
    def cancel(self, reason: str) -> None: ...
    @property
    def reason(self) -> str | None: ...
```

Cancellation reasons surface in error messages and audit events. Known reasons: `user_abort`, `timeout`, `api_disconnect`, `max_cost_exceeded`.

### `CheckpointerHandle`

A thin opaque wrapper around the LangGraph checkpointer. Core never touches LangGraph types; the handle exposes:

```python
class CheckpointerHandle(Protocol):
    async def put(self, key: str, value: bytes) -> None: ...
    async def get(self, key: str) -> bytes | None: ...
    async def list_keys(self, prefix: str) -> list[str]: ...
```

The adapter maps this to the underlying LangGraph checkpointer. Core never sees `langgraph.checkpoint.*` types.

## Messages

`FoundryMessage` is the provider-agnostic message shape. Provider adapters in `foundry.providers/*` convert to and from this type. Agents, tools, and the orchestration compiler only work with `FoundryMessage`.

```python
class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

class TextBlock(BaseModel):
    type: Literal["text"] = "text"
    text: str
    cache_control: CacheControl | None = None   # provider capability; see 11

class ToolUseBlock(BaseModel):
    type: Literal["tool_use"] = "tool_use"
    id: str
    name: str
    input: dict[str, Any]

class ToolResultBlock(BaseModel):
    type: Literal["tool_result"] = "tool_result"
    tool_use_id: str
    content: list["ContentBlock"]
    is_error: bool = False

class ImageBlock(BaseModel):
    type: Literal["image"] = "image"
    source_type: Literal["base64", "url"]
    media_type: str
    data: str

ContentBlock = Annotated[
    TextBlock | ToolUseBlock | ToolResultBlock | ImageBlock,
    Field(discriminator="type"),
]

class FoundryMessage(BaseModel):
    role: MessageRole
    content: list[ContentBlock]
```

### Why tagged unions

Content blocks are heterogeneous but must be serialisable (for run artifacts) and validatable at boundaries. Pydantic's discriminated unions give both for free. Adding a block type (e.g. `ThinkingBlock` when extended thinking ships) is additive; existing consumers pattern-match on `type`.

### Normalization rules

- Roles align with Anthropic's vocabulary (`system`/`user`/`assistant`/`tool`). OpenAI's additional roles (`developer`, `function`) are mapped by the OpenAI provider adapter.
- Tool use/result correlation uses `id` / `tool_use_id` — canonical foundry ids, not provider ids. The provider adapter translates on egress and on ingress.
- Images are always carried as base64 or URL; the foundry does not fetch or resize.

## `ModelResponse` and `ModelDelta`

```python
class StopReason(StrEnum):
    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    TOOL_USE = "tool_use"
    ERROR = "error"
    FILTERED = "filtered"          # content-policy stop

class TokenUsage(BaseModel):
    input_tokens: int
    output_tokens: int
    cached_read_tokens: int = 0
    cached_write_tokens: int = 0
    reasoning_tokens: int = 0
    """Tokens spent on hidden reasoning (OpenAI o-series, future Anthropic
    extended-thinking variants). Distinct from output_tokens — billed
    separately on most vendors. Defaults to 0 for models without
    reasoning."""

class ModelResponse(BaseModel):
    message: FoundryMessage
    stop_reason: StopReason
    usage: TokenUsage
    model: str
    provider: str
    latency_ms: int
    cost_estimate_usd: Decimal | None = None
    raw_provider_response: dict[str, Any] | None = None
    """Opaque passthrough for provider-specific fields.
    Never depended on by non-provider code."""

class ModelDelta(BaseModel):
    """One chunk of a streaming response."""
    content_block_index: int
    delta: TextBlock | ToolUseBlockDelta | None  # partial
    stop_reason: StopReason | None = None
    usage: TokenUsage | None = None              # typically only present on final delta
```

`ModelResponse.raw_provider_response` is the *only* officially-sanctioned channel for provider-specific data to leak past the adapter boundary. It is typed as an opaque dict, not surfaced in public foundry APIs, and consumers outside `foundry.providers/*` MUST NOT depend on its shape. It exists for audit / debug and for tests that assert provider-specific behaviour.

## Streaming events

The foundry streams richer information than just LLM tokens. Tool calls starting, tool results arriving, handoffs between agents, state transitions, approval requests — all these are observable progressively as a run unfolds. `RunEvent` is the tagged union every streaming surface (CLI `--stream`, API `POST /stream`, API WebSocket, meta-agent's interactive session, run artifact writer) serialises from.

### `RunEvent`

```python
class _RunEventBase(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: RunId
    sequence: int
    """Monotonically increasing within a run. Used by SSE Last-Event-ID
    resume semantics and by clients that need to detect gaps."""
    timestamp: datetime

class RunStarted(_RunEventBase):
    event: Literal["run.started"] = "run.started"
    project: str
    system_version: str            # git sha
    pin_set_hash: str
    inputs_hash: str

class AgentStarted(_RunEventBase):
    event: Literal["agent.started"] = "agent.started"
    agent_name: str
    agent_version: str

class AgentCompleted(_RunEventBase):
    event: Literal["agent.completed"] = "agent.completed"
    agent_name: str
    output_summary: str | None = None   # short, safe-to-log

class FunctionNodeStarted(_RunEventBase):
    event: Literal["function_node.started"] = "function_node.started"
    node_name: str
    node_version: str

class FunctionNodeCompleted(_RunEventBase):
    event: Literal["function_node.completed"] = "function_node.completed"
    node_name: str
    fields_written: list[str]
    bytes_delta: int
    latency_ms: int

class LLMCallStarted(_RunEventBase):
    event: Literal["llm.started"] = "llm.started"
    agent_name: str
    provider: str
    model: str
    prompt_tokens_estimate: int | None = None

class LLMDelta(_RunEventBase):
    event: Literal["llm.delta"] = "llm.delta"
    agent_name: str
    content_block_index: int
    delta: TextBlock | ToolUseBlockDelta | None
    """Provider-agnostic partial content chunk. Tool-use partials carry
    accumulated JSON input; full ModelDelta semantics live in model.py."""

class LLMCallCompleted(_RunEventBase):
    event: Literal["llm.completed"] = "llm.completed"
    agent_name: str
    usage: TokenUsage
    cost_estimate_usd: Decimal | None
    latency_ms: int
    stop_reason: StopReason

class ToolStarted(_RunEventBase):
    event: Literal["tool.started"] = "tool.started"
    agent_name: str
    tool_ref: str
    tool_version: str
    input_hash: str
    input_preview: str | None = None    # truncated, redacted

class ToolCompleted(_RunEventBase):
    event: Literal["tool.completed"] = "tool.completed"
    agent_name: str
    tool_ref: str
    tool_version: str
    success: bool
    latency_ms: int
    retry_count: int = 0
    output_preview: str | None = None   # truncated, redacted
    error_category: str | None = None

class ConnectionEvent(_RunEventBase):
    event: Literal["connection"] = "connection"
    agent_name: str
    connection_descriptor: ConnectionDescriptor
    lifecycle: Literal["acquire", "cache_hit", "refresh", "release", "evict", "health_check"]
    latency_ms: int

class EmbedCall(_RunEventBase):
    event: Literal["embed"] = "embed"
    agent_name: str
    embedder: str             # e.g. 'voyage:voyage-3'
    input_count: int
    input_tokens: int
    purpose: Literal["query", "document"]
    latency_ms: int
    cost_estimate_usd: Decimal | None

class SemanticCacheHitEvent(_RunEventBase):
    event: Literal["cache.semantic.hit"] = "cache.semantic.hit"
    agent_name: str
    similarity: float
    threshold: float
    cached_at: datetime
    saved_tokens_estimate: int
    saved_cost_estimate_usd: Decimal | None

class SemanticCacheMiss(_RunEventBase):
    event: Literal["cache.semantic.miss"] = "cache.semantic.miss"
    agent_name: str
    top_similarity: float      # best candidate seen, below threshold
    threshold: float

class SemanticCacheStore(_RunEventBase):
    event: Literal["cache.semantic.store"] = "cache.semantic.store"
    agent_name: str
    ttl_s: int

class ToolCacheHit(_RunEventBase):
    event: Literal["cache.tool.hit"] = "cache.tool.hit"
    agent_name: str
    tool_ref: str
    tool_version: str
    cached_at: datetime

class RetrievalEvent(_RunEventBase):
    event: Literal["retrieval"] = "retrieval"
    agent_name: str
    retriever: str             # e.g. 'pgvector_docs'
    kind: Literal["dense", "sparse", "hybrid"]
    top_k: int
    returned: int
    latency_ms: int

class MemoryRead(_RunEventBase):
    event: Literal["memory.read"] = "memory.read"
    agent_name: str
    layers_read: list[str]
    layers_failed: list[str] = Field(default_factory=list)
    total_tokens_estimate: int
    truncated: bool

class MemoryWriteEvent(_RunEventBase):
    event: Literal["memory.write"] = "memory.write"
    agent_name: str
    layer_name: str
    layer_kind: Literal["working", "episodic", "semantic", "custom"]
    write_kind: Literal["message", "summary", "fact", "raw"]
    bytes: int

class MemoryConsolidate(_RunEventBase):
    event: Literal["memory.consolidate"] = "memory.consolidate"
    agent_name: str
    layer_name: str
    trigger: Literal["periodic", "session_end", "explicit"]
    input_tokens_summarised: int
    output_tokens_written: int
    latency_ms: int

class RerankEvent(_RunEventBase):
    event: Literal["rerank"] = "rerank"
    agent_name: str
    reranker: str              # e.g. 'cohere:rerank-3'
    candidates: int
    top_k: int | None
    latency_ms: int
    cost_estimate_usd: Decimal | None

class Handoff(_RunEventBase):
    event: Literal["handoff"] = "handoff"
    from_agent: str
    to_agent: str
    trigger: Literal["rule", "llm", "end"]
    hop_number: int

class StateTransition(_RunEventBase):
    event: Literal["state.transition"] = "state.transition"
    agent_name: str
    fields_written: list[str]
    bytes_delta: int

class ApprovalRequired(_RunEventBase):
    event: Literal["approval.required"] = "approval.required"
    agent_name: str
    approval_id: str
    prompt: str
    context: dict[str, Any] = Field(default_factory=dict)

class ApprovalResolved(_RunEventBase):
    event: Literal["approval.resolved"] = "approval.resolved"
    approval_id: str
    decision: Literal["approved", "rejected"]
    reason: str | None = None

class RunCompleted(_RunEventBase):
    event: Literal["run.completed"] = "run.completed"
    status: Literal["success", "max_hops", "approval_pending"]
    final_output: Any | None = None
    total_input_tokens: int
    total_output_tokens: int
    total_cost_estimate_usd: Decimal | None
    duration_ms: int

class RunFailed(_RunEventBase):
    event: Literal["run.failed"] = "run.failed"
    error: dict[str, Any]          # FoundryError.to_dict()

class RunCancelled(_RunEventBase):
    event: Literal["run.cancelled"] = "run.cancelled"
    reason: str

RunEvent = Annotated[
    RunStarted | AgentStarted | AgentCompleted
    | FunctionNodeStarted | FunctionNodeCompleted
    | LLMCallStarted | LLMDelta | LLMCallCompleted
    | ToolStarted | ToolCompleted
    | ConnectionEvent
    | EmbedCall
    | SemanticCacheHitEvent | SemanticCacheMiss | SemanticCacheStore
    | ToolCacheHit
    | RetrievalEvent | RerankEvent
    | MemoryRead | MemoryWriteEvent | MemoryConsolidate
    | Handoff | StateTransition
    | ApprovalRequired | ApprovalResolved
    | RunCompleted | RunFailed | RunCancelled,
    Field(discriminator="event"),
]
```

### `InboundMessage`

Messages the client can send *into* a streaming run over WebSocket. SSE is unidirectional and does not carry these — separate POST endpoints correlate by `run_id` for SSE-mode clients.

```python
class _InboundBase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run_id: RunId
    client_sequence: int
    """Monotonic on the client side. Server echoes in any resulting
    RunEvent for correlation."""

class InjectInput(_InboundBase):
    kind: Literal["inject_input"] = "inject_input"
    message: FoundryMessage         # appended to conversation state on next node tick

class ApprovalResponse(_InboundBase):
    kind: Literal["approval_response"] = "approval_response"
    approval_id: str
    decision: Literal["approved", "rejected"]
    reason: str | None = None

class CancelRun(_InboundBase):
    kind: Literal["cancel"] = "cancel"
    reason: str = "user_abort"

class PauseRun(_InboundBase):
    kind: Literal["pause"] = "pause"

class ResumeRun(_InboundBase):
    kind: Literal["resume"] = "resume"

InboundMessage = Annotated[
    InjectInput | ApprovalResponse | CancelRun | PauseRun | ResumeRun,
    Field(discriminator="kind"),
]
```

### `Agent.astream()` (optional protocol method)

```python
@runtime_checkable
class Agent(Protocol):
    name: str
    version: str

    async def run(self, state: StateBase, session: Session) -> AgentResult: ...

    def astream(
        self,
        state: StateBase,
        session: Session,
    ) -> AsyncIterator[RunEvent]:
        """Stream progressive events. Agents that implement this override
        BaseAgent's default, which synthesises events around a single
        `run()` call (AgentStarted, wrapped LLM/tool events observed via
        hooks, AgentCompleted). Native astream is useful for agents whose
        internal loop emits events the hook-based synthesis cannot capture."""
```

### Event-stream invariants

1. **`sequence` is strictly monotonic within a run.** Skips mean a dropped event; clients can detect and reconnect. No duplicates.
2. **`RunStarted` is the first event; `RunCompleted` | `RunFailed` | `RunCancelled` is the last event.** No events follow a terminal event.
3. **Every `ToolStarted` has a matching `ToolCompleted`.** Same for `LLMCallStarted`/`LLMCallCompleted`, `AgentStarted`/`AgentCompleted`, `ApprovalRequired`/`ApprovalResolved`.
4. **Previews are redacted.** `input_preview` / `output_preview` / `final_output` honour the `ObservabilityConfig.capture_*` flags; when suppressed, the field is `None`, not a placeholder.
5. **Events are immutable once emitted.** No backfilling. Corrections are new events (`ToolCompleted` with `retry_count > 0` correcting an earlier failed attempt's recorded outcome).

### SSE envelope

Standard `text/event-stream`. Each event is JSON-serialised and framed as:

```
id: <sequence>
event: <event_discriminator>     # e.g. llm.delta
data: {"run_id": "...", "sequence": 42, "event": "llm.delta", ...}

```

(Double newline between events.) Clients implementing `Last-Event-ID` can reconnect and resume from the persisted run artifact — the API replays events from `sequence = Last-Event-ID + 1` onward.

### WebSocket envelope

Frames carry a discriminated JSON envelope in each direction:

```json
{"direction": "outbound", "event": {...RunEvent...}}
{"direction": "inbound",  "message": {...InboundMessage...}}
```

The server writes outbound events as they happen. Inbound messages are read in a separate task group branch and dispatched:
- `inject_input` → appended to the pending conversation state for the next node tick.
- `approval_response` → resolves the matching `ApprovalRequired`; emits `ApprovalResolved`; resumes the interrupted node.
- `cancel` → triggers `session.cancel_token.cancel(reason)`; surfaces as `RunCancelled`.
- `pause` / `resume` → toggles the orchestration layer's scheduling; no new node ticks while paused.

### Why a tagged union and not callbacks

Event streams compose. An API endpoint, a CLI, a notebook, the run-artifact writer, and a future TUI all subscribe to the same sequence. A callback design would couple producers to consumers; a typed tagged union lets anything that can read a Pydantic model participate. It also makes run artifacts and the OTel stream the same shape — `trace.jsonl` is `list[RunEvent]`.

## State primitives

```python
class Reducer(StrEnum):
    APPEND = "append"
    MERGE = "merge"
    LAST_WRITE_WINS = "last_write_wins"
    REPLACE_IF_SET = "replace_if_set"   # only replace if the new value is not None

class StateBase(BaseModel):
    """Base class for user-defined state schemas.

    Fields default to LAST_WRITE_WINS. Use Annotated[..., Reducer.X]
    to override:

        class MyState(StateBase):
            messages: Annotated[list[FoundryMessage], Reducer.APPEND]
            scratchpad: Annotated[dict[str, Any], Reducer.MERGE]
            final: str | None = None   # implicit LAST_WRITE_WINS
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        validate_assignment=True,
    )

    @classmethod
    def reducer_map(cls) -> dict[str, Reducer]: ...
```

`reducer_map` is introspected by the compiler (via `typing.get_type_hints(..., include_extras=True)`) to build the LangGraph state TypedDict with the correct reducer callables. See `22-state-management.md` for the full model; this section just establishes the type that underpins it.

## Exception hierarchy

```
FoundryError
├── ConfigError                    invalid YAML, schema mismatch, missing file
│   ├── ConfigLoadError
│   ├── ConfigValidationError
│   └── StateVisibilityError       compile-time: agent reads/writes forbidden fields
├── ProviderError                  anything originating from a provider call
│   ├── ProviderAuthError
│   ├── ProviderRateLimitError
│   ├── ProviderTimeoutError
│   ├── ProviderContentPolicyError
│   ├── ProviderConfigError        invalid model name, unsupported setting
│   └── ProviderUnexpectedError    catch-all for non-classified provider faults
├── ToolError                      raised by tool handlers or the tool layer
│   ├── ToolInputValidationError
│   ├── ToolOutputValidationError
│   ├── ToolHandlerError           handler raised an arbitrary exception
│   ├── ToolNotAllowedError        agent's allowlist rejected the call
│   └── ToolNotFoundError          registry did not resolve the ref
├── OrchestrationError
│   ├── UnknownPatternError
│   ├── CompileError               graph compilation failed (ref, edge, schema)
│   ├── CyclicDependencyError
│   ├── MaxHopsExceededError
│   └── CostBudgetExceeded         next call would breach Guardrails.max_cost_usd
├── CheckpointError
│   ├── CheckpointWriteError
│   └── CheckpointReadError
├── ConnectionError                anything about external-system connections
│   ├── ConnectionConfigError      bad config shape / invalid settings
│   ├── ConnectionAuthError        auth failed (401, expired token, bad cert, etc.)
│   ├── ConnectionTimeoutError     factory/health exceeded budget
│   ├── ConnectionHealthCheckError ran, came back unhealthy
│   ├── ConnectionPoolExhausted    concurrency-limit ceiling hit
│   ├── ConnectionSlotNotDeclaredError   tool asked for a slot it didn't declare
│   ├── ConnectionSlotNotBoundError      project didn't bind a declared slot
│   └── ConnectionRefreshError     token/cert refresh itself failed
├── EmbedderError                  anything about embedding calls
│   ├── EmbedderConfigError        unsupported model, invalid input size
│   ├── EmbedderAuthError          credentials rejected
│   ├── EmbedderTimeoutError       call exceeded budget
│   └── EmbedderUnexpectedError    catch-all
├── CacheError                     anything about cache operations
│   ├── CacheBackendError          backing store unavailable (redis down, etc.)
│   └── CacheCorruptedEntry        stored entry fails schema validation on read
├── MemoryError                    anything about memory subsystem
│   ├── MemoryConfigError          bad layer config / circular references
│   ├── MemoryLayerError           one layer failed (degrades gracefully unless strict)
│   └── MemoryConsolidateError     consolidator LLM/state write failed
├── ApprovalRequired               NOT AN ERROR — raised to signal HITL pause
├── RunCancelled                   session cancellation propagated
└── VersioningError
    ├── RefResolutionError
    ├── PinConflictError
    └── RollbackError
```

### Rules

- Every exception subclasses `FoundryError`. No arbitrary `Exception`s cross public API boundaries — wrap before re-raising.
- Error messages MUST be structured: `FoundryError` exposes a `to_dict()` method that includes `error_class`, `message`, `context` (a `dict[str, Any]` of diagnostic fields), and optionally `cause_chain`.
- `ApprovalRequired` is not a true error — it's a control-flow exception. The runtime catches it, persists the pending-approval state, and surfaces it through the API/CLI. Tool handlers raise it when they need human approval before proceeding.
- `RunCancelled` is raised when `session.cancel_token` transitions to cancelled. Tool handlers and agent steps should let it propagate without catching.

### to_dict contract

```python
class FoundryError(Exception):
    def __init__(
        self,
        message: str,
        *,
        context: dict[str, Any] | None = None,
        cause: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.context = context or {}
        self.__cause__ = cause

    def to_dict(self) -> dict[str, Any]:
        return {
            "error_class": type(self).__name__,
            "message": str(self),
            "context": self.context,
            "cause_chain": _walk_causes(self.__cause__) if self.__cause__ else [],
        }
```

This is what the API layer serialises to JSON on failed requests and what the observability layer writes into the `llm_calls` / `tool_calls` error fields.

## Async runtime rules

1. **Single event loop per process.** The foundry never calls `asyncio.run` internally except at CLI entrypoints. FastAPI uses uvicorn's loop; notebooks use Jupyter's; tests use `pytest-asyncio`'s.
2. **`anyio` over `asyncio` where possible.** `anyio.to_thread.run_sync`, `anyio.fail_after`, `anyio.create_task_group` — gives us trio compatibility at zero cost and a cleaner cancellation story.
3. **No blocking calls in async code.** Tool handlers that need to do CPU-bound or blocking I/O work MUST wrap with `anyio.to_thread.run_sync(...)`. A lint rule (`ASYNC100`-style) is on by default and warns on synchronous `requests`/`time.sleep`/`open` inside `async def`.
4. **Timeouts via `anyio.fail_after`.** Every provider call, tool call, and agent step has an explicit timeout resolved from config; the timeout is enforced with `fail_after` around the actual `await`.
5. **Cancellation is cooperative.** When `session.cancel_token.cancel(reason)` is called, the surrounding `anyio.CancelScope` is triggered; running `await`s raise `CancelledError` which surfaces as `RunCancelled` at the foundry boundary.
6. **Structured concurrency for fan-out.** Parallel agent nodes use `anyio.create_task_group` (not bare `asyncio.gather`) so that an exception in one branch cancels the siblings cleanly.

## Lifecycle sequence

For a single-agent run (multi-agent adds more node/handoff spans):

```
session.start
├── hooks.before_run
├── span(foundry.run)
│   ├── compile check (cheap; fast fail on ref errors)
│   ├── checkpointer.restore_or_init
│   ├── span(foundry.node, agent=X)
│   │   ├── hooks.before_node
│   │   ├── agent._step (may call LLM, may call tools)
│   │   │   ├── span(foundry.llm, model=...)
│   │   │   └── span(foundry.tool, tool=...) (0..n)
│   │   │       ├── hooks.before_tool
│   │   │       ├── tool.handle (inside anyio.fail_after(timeout))
│   │   │       └── hooks.after_tool
│   │   └── hooks.after_node
│   ├── checkpointer.checkpoint
│   └── span close
└── hooks.after_run
```

Every span is emitted. Every `await` respects cancellation. Every exception has its `to_dict()` called by the observability writer before propagating.

## Public API surface

`foundry.core.__init__.py` re-exports exactly this set. The foundry's top-level `__init__.py` re-exports from `foundry.core` plus a curated subset of other modules. This is what `from foundry import X` resolves.

```python
# src/foundry/core/__init__.py
from .node import Node, NodeResult
from .agent import Agent, AgentResult, BaseAgent, LifecycleHooks
from .function_node import FunctionNode, BaseFunctionNode
from .tool import Tool, BaseTool, ToolRegistry, RunContext, RetryPolicy
from .connection import (
    Connection, ConnectionFactory, ConnectionPool, ConnectionAccessor,
    ConnectionHealth, ConnectionDescriptor, ConnectionContext,
    AuthScheme,
)
from .embedder import Embedder, Embedding, EmbedderCapabilities, EmbedderPricing
from .retrieval import Retriever, Reranker, RetrievedDocument
from .memory import (
    Memory, MemoryLayer, MemoryContext,
    MemoryEnvelope, MemoryContribution, MemoryWrite,
)
from .cache import (
    SemanticCache, SemanticCacheKey, SemanticCacheHit,
    ResultCache, CacheAccessor,
)
from .session import (
    Session, RunId, CancelToken, CheckpointerHandle, CostBudget,
)
from .messages import (
    FoundryMessage, MessageRole,
    ContentBlock, TextBlock, ToolUseBlock, ToolResultBlock, ImageBlock,
    CacheControl,
)
from .model import ModelResponse, ModelDelta, StopReason, TokenUsage
from .events import (
    RunEvent,
    RunStarted, AgentStarted, AgentCompleted,
    LLMCallStarted, LLMDelta, LLMCallCompleted,
    ToolStarted, ToolCompleted, ConnectionEvent,
    Handoff, StateTransition,
    ApprovalRequired, ApprovalResolved,
    RunCompleted, RunFailed, RunCancelled,
    InboundMessage, InjectInput, ApprovalResponse,
    CancelRun, PauseRun, ResumeRun,
)
from .state import StateBase, Reducer
from .errors import (
    FoundryError,
    ConfigError, ConfigLoadError, ConfigValidationError, StateVisibilityError,
    ProviderError, ProviderAuthError, ProviderRateLimitError,
    ProviderTimeoutError, ProviderContentPolicyError, ProviderConfigError,
    ProviderUnexpectedError,
    ToolError, ToolInputValidationError, ToolOutputValidationError,
    ToolHandlerError, ToolNotAllowedError, ToolNotFoundError,
    OrchestrationError, UnknownPatternError, CompileError,
    CyclicDependencyError, MaxHopsExceededError,
    CheckpointError, CheckpointWriteError, CheckpointReadError,
    ApprovalRequired, RunCancelled,
    VersioningError, RefResolutionError, PinConflictError, RollbackError,
)

__all__ = [
    "Node", "NodeResult",
    "Agent", "AgentResult", "BaseAgent", "LifecycleHooks",
    "FunctionNode", "BaseFunctionNode",
    "Tool", "BaseTool", "ToolRegistry", "RunContext", "RetryPolicy",
    "Session", "RunId", "CancelToken", "CheckpointerHandle", "CostBudget",
    "FoundryMessage", "MessageRole",
    "ContentBlock", "TextBlock", "ToolUseBlock", "ToolResultBlock", "ImageBlock",
    "CacheControl",
    "ModelResponse", "ModelDelta", "StopReason", "TokenUsage",
    "RunEvent",
    "RunStarted", "AgentStarted", "AgentCompleted",
    "FunctionNodeStarted", "FunctionNodeCompleted",
    "LLMCallStarted", "LLMDelta", "LLMCallCompleted",
    "ToolStarted", "ToolCompleted", "ConnectionEvent",
    "EmbedCall",
    "SemanticCacheHitEvent", "SemanticCacheMiss", "SemanticCacheStore",
    "ToolCacheHit",
    "RetrievalEvent", "RerankEvent",
    "MemoryRead", "MemoryWriteEvent", "MemoryConsolidate",
    "Handoff", "StateTransition",
    "ApprovalRequired", "ApprovalResolved",
    "RunCompleted", "RunFailed", "RunCancelled",
    "InboundMessage", "InjectInput", "ApprovalResponse",
    "CancelRun", "PauseRun", "ResumeRun",
    "Embedder", "Embedding", "EmbedderCapabilities", "EmbedderPricing",
    "Retriever", "Reranker", "RetrievedDocument",
    "Memory", "MemoryLayer", "MemoryContext",
    "MemoryEnvelope", "MemoryContribution", "MemoryWrite",
    "SemanticCache", "SemanticCacheKey", "SemanticCacheHit",
    "ResultCache", "CacheAccessor",
    "StateBase", "Reducer",
    # errors
    "FoundryError",
    "ConfigError", "ConfigLoadError", "ConfigValidationError", "StateVisibilityError",
    "ProviderError", "ProviderAuthError", "ProviderRateLimitError",
    "ProviderTimeoutError", "ProviderContentPolicyError", "ProviderConfigError",
    "ProviderUnexpectedError",
    "ToolError", "ToolInputValidationError", "ToolOutputValidationError",
    "ToolHandlerError", "ToolNotAllowedError", "ToolNotFoundError",
    "OrchestrationError", "UnknownPatternError", "CompileError",
    "CyclicDependencyError", "MaxHopsExceededError", "CostBudgetExceeded",
    "CheckpointError", "CheckpointWriteError", "CheckpointReadError",
    "ConnectionError", "ConnectionConfigError", "ConnectionAuthError",
    "ConnectionTimeoutError", "ConnectionHealthCheckError",
    "ConnectionPoolExhausted", "ConnectionSlotNotDeclaredError",
    "ConnectionSlotNotBoundError", "ConnectionRefreshError",
    "EmbedderError", "EmbedderConfigError", "EmbedderAuthError",
    "EmbedderTimeoutError", "EmbedderUnexpectedError",
    "CacheError", "CacheBackendError", "CacheCorruptedEntry",
    "MemoryError", "MemoryConfigError", "MemoryLayerError",
    "MemoryConsolidateError",
    "ApprovalRequired", "RunCancelled",
    "VersioningError", "RefResolutionError", "PinConflictError", "RollbackError",
    # connection primitives
    "Connection", "ConnectionFactory", "ConnectionPool", "ConnectionAccessor",
    "ConnectionHealth", "ConnectionDescriptor", "ConnectionContext", "AuthScheme",
]
```

No `langgraph`, no `langchain_core`, no `anthropic`, no `openai` types appear here. Enforced by the import-boundary lint.

## Invariants

Hard rules that every implementation must respect. Any PR violating an invariant is a bug.

1. **Core imports only stdlib + `pydantic` + `anyio`.** Lint-enforced.
2. **Protocols over ABCs for public contracts** (Agent, Tool). Makes test doubles cheap.
3. **Every `Session` is immutable after construction.** Mutation-looking operations (span entry, logger binding) produce new Sessions or context-managed lexical scopes.
4. **Every exception crossing a module boundary is a `FoundryError` subclass.** Wrap third-party exceptions at the nearest boundary; never let them leak.
5. **Every `await` either has a timeout above it (`anyio.fail_after`) or is inside a path whose caller is guaranteed to have one.** Unbounded waits are bugs.
6. **Every public `async def` is cancellation-safe.** No shielded waits without an explicit reason documented in code.
7. **No `raw_provider_response` reads outside `foundry.providers/*`.** Enforced by code review.
8. **`RunContext` does not escape the tool handler call** that received it. Storing it on `self` or in a module-level dict is a bug.
9. **`ApprovalRequired` is raised, never returned.** Control flow, not data flow.
10. **`to_dict()` exists on every `FoundryError` and returns JSON-serialisable content only.** Nested exceptions recursively serialise. Enforced by a test that checks `json.dumps(e.to_dict())` on every subclass.
11. **Connections acquired via `RunContext` are never closed by the handler.** The pool owns lifecycle. Handlers that explicitly call `conn.close()` or wrap a connection in `async with` are bugs.
12. **`ConnectionDescriptor` is the only connection data surfaced to logs / traces / error messages.** Raw clients, auth tokens, or resolved credentials never appear in observability output.

## Failure modes (how each layer fails)

| Failure | Surfaced as | Where caught |
|---|---|---|
| YAML unreadable or invalid | `ConfigLoadError` | `foundry.config.loader` |
| Schema violation in config | `ConfigValidationError` | Pydantic at loader |
| Agent reads a forbidden state field | `StateVisibilityError` at compile time | `foundry.orchestration.state_scope` |
| Provider call fails (5xx, timeout, 429) | Corresponding `ProviderError` subclass | provider adapter |
| Tool input doesn't match schema | `ToolInputValidationError` | `foundry.core.tool` dispatcher |
| Tool handler raises | Wrapped as `ToolHandlerError` | `foundry.core.tool` dispatcher |
| Tool output doesn't match schema | `ToolOutputValidationError` | dispatcher |
| Agent calls tool not in its allowlist | `ToolNotAllowedError` | dispatcher |
| Unresolvable tool ref | `ToolNotFoundError` | registry |
| HITL pause | `ApprovalRequired` raised, caught by orchestration layer, persisted to checkpoint, surfaced via run-status API |
| Run cancelled | `RunCancelled` | boundary (API/CLI) — returned with 499 equivalent |
| Max hops in orchestration | `MaxHopsExceededError` | orchestration runtime |

## Test expectations

The core module has these required tests. Each exit gate for Phase 1 includes running these.

### Unit

1. **Protocol compliance.** For every `BaseAgent` and `BaseTool` subclass in the codebase, assert `isinstance(instance, Agent)` / `isinstance(instance, Tool)`.
2. **`FoundryMessage` round-trips.** Construct a message with every block type, serialise with `model_dump_json`, parse with `model_validate_json`, assert equality.
3. **Exception `to_dict` is JSON-serialisable.** For every subclass of `FoundryError`, instantiate with a representative cause chain and assert `json.dumps(e.to_dict())` does not raise.
4. **`Session` frozen.** Assert `Session(...).run_id = ...` raises.
5. **`RunContext` frozen.** Same.
6. **Reducer introspection.** Given a `StateBase` subclass with mixed `Annotated[..., Reducer.X]` fields, `reducer_map()` returns the correct dict, and unannotated fields default to `LAST_WRITE_WINS`.
7. **`RunId.new()` monotonicity.** Generate 1000 ids in sequence; assert they sort ascending.
8. **Cancel token.** Async test: start a task that awaits `cancel_token.wait_cancelled`, call `cancel("reason")` from another task, assert the first task resolves and `cancelled()` is True and `reason == "reason"`.

### Contract

1. **Import boundary.** `ruff check src/foundry/core/` exits 0.
2. **No third-party leak in public API.** `python -c "import foundry.core; ..."` — assert no module in `foundry.core.__all__` is a `langgraph_*`, `langchain_*`, `anthropic`, or `openai` subclass.

### Integration (piggybacks on Phase 1 exit gate)

1. **`BaseAgent` lifecycle.** Construct a `BaseAgent` with all hooks set to record calls; run it; assert hooks were called in the documented order.

## Implementation notes (non-normative)

- **Pydantic config.** Use `ConfigDict(arbitrary_types_allowed=True, frozen=True)` on Session and RunContext. Use `strict=True` for primitive fields where applicable.
- **ULID library.** Recommend `python-ulid`; it's small and maintained. If we want zero deps, hand-roll the 26-char encoder — ~30 lines.
- **Structlog vs loguru for the bound logger.** Recommend `structlog` because it integrates cleanly with OTel via its `add_log_level` / `TimeStamper` processors. Loguru's monolithic config is nice but less composable.
- **`@runtime_checkable` cost.** Runtime protocol checks walk the type's MRO. Fine for boundary checks; don't use in hot loops.
- **Forward references in `ContentBlock`.** Use `from __future__ import annotations` + a `model_rebuild()` call at the end of `messages.py` to resolve the recursive `ToolResultBlock.content: list[ContentBlock]`.

## Open questions

1. **Do we want a `CostBudget` primitive in core?** A run-level budget that the provider layer decrements. Could raise `CostBudgetExceeded` pre-call. Rough analog: `MaxHopsExceededError`. Useful for meta-agent `forge` runs to hard-cap spend. Lean: yes, minimal primitive here, implemented in providers and observability.
2. **Do state fields support user-defined reducer callables, not just the enum?** The enum (4 reducer types) covers ~90% of cases. Custom reducers (e.g. "keep top-10 by score") would need `Annotated[T, CustomReducer(fn)]`. Decision deferred to `22-state-management.md`.
3. **Do we need an `AsyncIterator`-returning `stream()` on the Agent protocol?** Streaming exists at the provider layer; whether the agent layer exposes it is a UX question. Lean: no in v1 — agents are request/response; streaming is a cross-cutting concern at the runtime layer (it routes provider deltas up to the CLI/API directly).
4. **Exception compound vs individual classes.** We have ~25 error subclasses. One school: one class per failure mode (current). Other school: one class with an `error_code` field. Current choice wins on IDE autocomplete and isinstance clarity. Revisit if the hierarchy balloons past ~40.
