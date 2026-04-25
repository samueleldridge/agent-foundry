# 11 — Provider Abstraction

## Purpose

The provider abstraction is the single place the foundry knows about specific LLM vendors. It turns provider heterogeneity into a narrow, typed interface that the rest of the foundry programs against. Two load-bearing properties:

1. **Swapping providers is a config edit.** An agent that runs against Anthropic MUST also run against OpenAI, Bedrock, or Azure with only a `model_binding:` change in its YAML. No Python edits.
2. **Provider-specific features are reachable without leaking provider types.** Anthropic's cache-control, extended thinking, OpenAI's structured outputs, reasoning effort — each is expressed through a typed **capability** on the foundry side and translated into provider kwargs inside the adapter.

These two properties are in tension. The resolution: a lowest-common-denominator `Provider` interface *plus* a capabilities system that agents can declare they need and the foundry checks at compile time.

## Module layout

```
src/foundry/providers/
├── __init__.py              Provider, ProviderCapabilities, ModelBinding, ModelSettings
├── _base.py                 abstract ProviderAdapter base class
├── _registry.py             name → ProviderAdapter lookup
├── _langchain_bridge.py     wraps init_chat_model; the only place langchain_* is imported
├── anthropic.py             AnthropicProvider — cache_control, extended thinking
├── openai.py                OpenAIProvider — structured outputs, reasoning effort
├── bedrock.py               BedrockProvider — Anthropic-on-Bedrock + others
├── azure.py                 AzureOpenAIProvider — OpenAI-on-Azure
├── vertex.py                VertexProvider — Gemini + Anthropic-on-Vertex (deferred if risky)
├── pricing.py               per-model cost estimation
├── errors.py                maps provider exceptions → FoundryError subclasses
├── streaming.py             ModelDelta assembly for each provider's streaming format
├── rate_limit.py            RateLimiter protocol + InProcessTokenBucket + RedisTokenBucket
└── embedders/
    ├── __init__.py          EmbedderAdapter, EmbedderBinding, registry lookup
    ├── _base.py             abstract EmbedderAdapter base class
    ├── voyage.py            VoyageEmbedder — voyage-3, voyage-large (asymmetric q/d)
    ├── openai.py            OpenAIEmbedder — text-embedding-3-small / -large
    ├── cohere.py            CohereEmbedder — embed-v3 (asymmetric q/d, multilingual)
    ├── bedrock.py           BedrockEmbedder — titan-embed, cohere-on-bedrock
    └── manifests/           per-vendor embedder manifests (dims, max tokens, pricing)
```

## What `providers` imports

- `foundry.core` (types only).
- `pydantic`, `anyio`, stdlib.
- `langchain_core` and `langchain_*` (via `_langchain_bridge.py` only).
- Provider SDKs only where necessary for error typing (e.g. `anthropic.BadRequestError`) — not for generation calls, which go via LangChain.

## What imports `providers`

- `foundry.orchestration` (resolving a `ModelBinding` to a `Provider` at compile).
- `foundry.eval` (for `llm_judge` scorer).
- `foundry.configurator` (meta-agent is an agent; agents use providers).

No downstream module that is *not* in the list above imports `foundry.providers`. In particular, user-authored agents go through config, not through direct provider imports.

## Tour of the key types

| Type | Purpose |
|---|---|
| `Provider` | Protocol: `generate()` + `stream()` + `capabilities` + `name`. |
| `ProviderAdapter` | Abstract base class that every concrete provider subclasses. Lives in `_base.py`. |
| `ModelBinding` | Pydantic: provider + model + settings + declared-capabilities. Appears in `AgentSpec`. |
| `ModelSettings` | Provider-neutral settings (temperature, max_tokens, top_p, stop, response_format). |
| `ProviderCapabilities` | Static descriptor per provider+model: what features this combo supports. |
| `CapabilityRequirement` | Per-agent declaration: which capabilities the agent needs. Compile-time check. |
| `ModelRegistry` | Maps `provider + model` to a `ProviderCapabilities` record, loaded from `pricing.py` and per-provider manifests. |

## The `Provider` protocol

```python
@runtime_checkable
class Provider(Protocol):
    name: str
    """Canonical provider name, e.g. 'anthropic', 'openai', 'bedrock'."""

    model: str
    """The model id this provider instance is bound to."""

    capabilities: ProviderCapabilities
    """Static descriptor of what this provider+model supports."""

    async def generate(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema],
        settings: ResolvedModelSettings,
    ) -> ModelResponse: ...

    async def stream(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema],
        settings: ResolvedModelSettings,
    ) -> AsyncIterator[ModelDelta]: ...
```

### Why the same interface for stream and generate

`stream()` is what LangGraph calls when the foundry's CLI/API is running in streaming mode. `generate()` is what everything else calls. Implementations of `generate()` MAY internally call `stream()` and assemble the full response — this is the reference default in `_base.py`. Providers that have a meaningfully-faster non-streaming path (rare) can override.

### `ToolSchema`

A small, provider-neutral shape passed into `generate`/`stream`:

```python
class ToolSchema(BaseModel):
    name: str
    description: str
    input_schema: dict[str, Any]   # JSON Schema
```

The adapter converts this to the provider's native tool definition (Anthropic's `tools=[...]`, OpenAI's `tools=[{"type":"function",...}]`, etc.).

## `ModelBinding`

The Pydantic model that appears inside an `AgentSpec` and pins everything needed to locate, configure, and call a specific model:

```python
class ModelBinding(BaseModel):
    provider: str
    """Canonical provider name. Validated against the registry at load."""

    model: str
    """Model id, as the provider expects. e.g. 'claude-opus-4-7',
    'gpt-5', 'anthropic.claude-opus-4-7-v1:0' (Bedrock)."""

    settings: ModelSettings = Field(default_factory=ModelSettings)
    """Provider-neutral knobs."""

    capabilities_required: list[CapabilityName] = Field(default_factory=list)
    """Declared capabilities this binding must support. Checked at compile time
    against the registry. If a binding declares 'cache_control' but the
    provider+model doesn't support it, compilation fails."""

    provider_overrides: dict[str, Any] = Field(default_factory=dict)
    """ESCAPE HATCH. Raw kwargs forwarded to the provider. Use sparingly;
    documented as unportable. Meta-agent is prompted not to populate this
    unless explicitly instructed."""

    credentials_ref: CredentialsRef | None = None
    """Reference to a secret. None means 'use default env lookup'."""
```

### Capability-required checking

At `SystemSpec` compile time, for every agent's `ModelBinding`:

1. Resolve `provider + model` in the registry → `ProviderCapabilities`.
2. For each name in `capabilities_required`, assert the capabilities record has it set to `True`.
3. If any mismatch, raise `ProviderConfigError` with the failing capability + the list of providers/models that DO support it, as a hint.

This gives us: swap `provider: anthropic` for `provider: bedrock` at runtime and the load immediately tells you "your agent requires `extended_thinking` which is supported on anthropic but not on bedrock as of 2026-04." Config errors for free, no runtime surprises.

## `ModelSettings`

Provider-neutral knobs. Any setting that more than one provider honours goes here. Provider-specific knobs use the capabilities system or `provider_overrides`.

```python
class ModelSettings(BaseModel):
    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop_sequences: list[str] = Field(default_factory=list)
    response_format: ResponseFormat | None = None
    seed: int | None = None              # honoured by some providers for reproducibility
    reasoning_effort: ReasoningEffort | None = None    # requires 'reasoning_effort' capability
    thinking_budget_tokens: int | None = None          # requires 'extended_thinking' capability
    cache_control: CacheControlMode | None = None      # requires 'cache_control' capability
    timeout_s: float | None = None                     # wall-clock for one call
```

```python
class ResponseFormat(BaseModel):
    """Provider-neutral structured-output request. Requires 'structured_outputs'."""
    type: Literal["json", "json_schema"] = "json"
    schema: dict[str, Any] | None = None

class ReasoningEffort(StrEnum):
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"

class CacheControlMode(StrEnum):
    OFF = "off"
    SYSTEM = "system"                    # mark system prompt as cacheable
    SYSTEM_AND_TOOLS = "system_and_tools"  # + tool defs
    AGGRESSIVE = "aggressive"            # + prior messages (up to provider limits)
```

`ResolvedModelSettings` is what the provider receives after `ModelBinding.settings` is merged with `provider_overrides` and session-level overrides. The provider implementation works only with the resolved form.

## `ProviderCapabilities`

The static descriptor. Populated from a manifest per provider + model at foundry startup; user-overridable for new models the foundry doesn't know about yet.

```python
class CapabilityName(StrEnum):
    CACHE_CONTROL = "cache_control"
    EXTENDED_THINKING = "extended_thinking"
    REASONING_EFFORT = "reasoning_effort"
    STRUCTURED_OUTPUTS = "structured_outputs"
    VISION = "vision"
    TOOL_USE = "tool_use"
    TOOL_CHOICE = "tool_choice"
    STREAMING = "streaming"
    SEED = "seed"
    PREFILL = "prefill"
    LOGPROBS = "logprobs"
    PDF_INPUT = "pdf_input"

class ProviderCapabilities(BaseModel):
    provider: str
    model: str
    max_context_tokens: int
    max_output_tokens: int

    # capability flags
    cache_control: bool = False
    extended_thinking: bool = False
    reasoning_effort: bool = False
    structured_outputs: bool = False
    vision: bool = False
    tool_use: bool = True              # baseline
    tool_choice: bool = True
    streaming: bool = True
    seed: bool = False
    prefill: bool = False
    logprobs: bool = False
    pdf_input: bool = False

    pricing: ModelPricing
    """Per-1M-token prices; used for cost estimation."""

    def supports(self, name: CapabilityName) -> bool:
        return bool(getattr(self, name.value))
```

### Model manifest

Per-provider JSON files under `src/foundry/providers/manifests/<provider>.json`:

```json
{
  "anthropic": {
    "claude-opus-4-7": {
      "max_context_tokens": 200000,
      "max_output_tokens": 8192,
      "cache_control": true,
      "extended_thinking": true,
      "vision": true,
      "tool_use": true,
      "tool_choice": true,
      "streaming": true,
      "prefill": true,
      "pricing": { "input_per_1m": 15.0, "output_per_1m": 75.0,
                   "cache_read_per_1m": 1.5, "cache_write_per_1m": 18.75 }
    }
  }
}
```

Manifests are part of the foundry source. Adding a new model means adding a manifest entry + testing. Users can override via `~/.foundry/model_overrides.json` for models the shipped foundry doesn't know (e.g. a new private Bedrock model id).

## Concrete providers

Each concrete provider subclasses `ProviderAdapter` and implements:
- `_build_chat_model()` — returns the LangChain `BaseChatModel` via `init_chat_model` (or a custom client where necessary).
- `_to_provider_messages()` — `list[FoundryMessage]` → provider-native messages.
- `_from_provider_response()` — provider response → `ModelResponse`.
- `_stream_deltas()` — streaming chunk → `ModelDelta` translator.
- `_classify_error()` — provider exception → `ProviderError` subclass.

The base class does the heavy lifting (spans, timing, cost, retries, logging) and calls into these hooks. Concrete providers stay small — ~150–250 lines each.

### Anthropic

- Uses `langchain_anthropic` under the hood.
- `cache_control` → marks blocks with `{"type": "text", "text": ..., "cache_control": {"type": "ephemeral"}}`. Foundry's `TextBlock.cache_control: CacheControl | None` is the typed wrapper.
- `extended_thinking` → adds `thinking={"type": "enabled", "budget_tokens": N}` when `settings.thinking_budget_tokens` is set.
- `prefill` → trailing assistant message is treated as a prefill.
- Tool use: native; `tool_choice` supports `{"type": "auto" | "any" | "tool"}`.
- Error classification:
  - `anthropic.AuthenticationError` → `ProviderAuthError`
  - `anthropic.RateLimitError` → `ProviderRateLimitError`
  - `anthropic.APITimeoutError` → `ProviderTimeoutError`
  - `anthropic.BadRequestError` with content-policy reason → `ProviderContentPolicyError`
  - Other `anthropic.APIError` → `ProviderUnexpectedError` with `context={provider_error_type: ...}`.
- Streaming: Anthropic's SSE event types (`message_start`, `content_block_start`, `content_block_delta`, `content_block_stop`, `message_delta`, `message_stop`) map to `ModelDelta` per block.
- Usage extraction: `response.usage` → `TokenUsage` including cache read/write.

### OpenAI

- Uses `langchain_openai`.
- `structured_outputs` → `response_format={"type":"json_schema", "json_schema":...}` path when `settings.response_format.type == "json_schema"`.
- `reasoning_effort` → `reasoning={"effort":"..."}` for reasoning models (o-series and newer). Capability-gated.
- Tool use: function-calling format; translate foundry `ToolSchema` to `{"type":"function","function":{"name","description","parameters"}}`.
- Error classification:
  - `openai.AuthenticationError` → `ProviderAuthError`
  - `openai.RateLimitError` → `ProviderRateLimitError`
  - `openai.APITimeoutError` → `ProviderTimeoutError`
  - `openai.BadRequestError` where `code == "content_filter"` → `ProviderContentPolicyError`
- Streaming: OpenAI's chunk format → `ModelDelta`.

### Bedrock

- Uses `langchain_aws` (`ChatBedrockConverse` or `ChatBedrock`).
- Supports Anthropic, Meta, Mistral, and others hosted on Bedrock.
- Model IDs use the Bedrock format: `anthropic.claude-opus-4-7-v1:0`.
- Capability parity: for Anthropic-on-Bedrock, cache-control *may* be supported depending on region + Bedrock rollout — the manifest reflects current status; defaults to `false` until verified. This is exactly the scenario where the capability-required check earns its keep: declaring `cache_control: true` in the binding + swapping `provider: anthropic → bedrock` fails compile if the Bedrock model variant doesn't advertise it.
- Credentials: SigV4 via the default boto3 chain. A `CredentialsRef` can point at a named AWS profile or assumed-role spec.

### Azure OpenAI

- Uses `langchain_openai` with `AzureChatOpenAI`.
- Deployment name ≠ model id; the manifest keys by Azure-deployment-name convention but the capabilities match the underlying OpenAI model.
- Reasoning and structured outputs work identically to OpenAI.

### Vertex

- Gemini models + Anthropic-on-Vertex.
- Deferred to Phase 1 polish if integration proves thorny; contract is the same as the others.

## Embedders (separate adapter family)

Embedding endpoints are distinct from generation endpoints — different APIs, different vendor strengths, different pricing. Embedders get their own adapter family alongside `ProviderAdapter`, with an analogous shape.

### `EmbedderBinding`

```python
class EmbedderBinding(BaseModel):
    provider: str
    """Canonical embedder provider: 'voyage', 'openai', 'cohere', 'bedrock'."""

    model: str
    """Model id, e.g. 'voyage-3', 'text-embedding-3-small', 'embed-english-v3.0'."""

    settings: EmbedderSettings = Field(default_factory=EmbedderSettings)
    credentials_ref: CredentialsRef | None = None

class EmbedderSettings(BaseModel):
    batch_size: int = 64
    timeout_s: float = 30.0
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
```

`EmbedderBinding` appears in `AgentSpec.semantic_cache.embedder_binding` and in retriever/reranker catalog configs. Same pattern as `ModelBinding` — provider-agnostic, pluggable, compile-time validated.

### Concrete embedders

| Provider | Models | Asymmetric q/d | Batch | Notes |
|---|---|---|---|---|
| **Voyage** | `voyage-3`, `voyage-3-large`, `voyage-code-3` | ✅ | up to 128 | Recommended partner for Anthropic deployments (no native Anthropic embedder as of 2026). |
| **OpenAI** | `text-embedding-3-small` (1536), `text-embedding-3-large` (3072, configurable down) | ❌ | up to 2048 | Native to OpenAI / Azure OpenAI. Dimension truncation via `dimensions` param. |
| **Cohere** | `embed-english-v3.0`, `embed-multilingual-v3.0` | ✅ | up to 96 | Strong multilingual. Pairs well with Cohere Rerank. |
| **Bedrock** | `amazon.titan-embed-text-v2`, `cohere.embed-*` | varies | varies | SigV4 auth; same `aws_session` connection pattern as Bedrock generation. |

### `EmbedderAdapter` base

```python
class EmbedderAdapter(ABC):
    name: ClassVar[str]
    capabilities: EmbedderCapabilities

    @abstractmethod
    def _build_client(self) -> Any: ...

    @abstractmethod
    async def _embed_batch(
        self,
        texts: list[str],
        purpose: Literal["query", "document"],
    ) -> list[list[float]]: ...

    @abstractmethod
    def _classify_error(self, exc: Exception) -> EmbedderError | None: ...

    async def embed(
        self,
        inputs: list[str],
        purpose: Literal["query", "document"] = "document",
    ) -> list[Embedding]:
        """Default impl: chunks inputs into batches of
        capabilities.max_batch_size, dispatches concurrently via
        create_task_group, applies retries + rate limiting + timeout,
        emits foundry.embed events, returns ordered results."""
        ...
```

The base does retries, rate limiting, per-attempt timeouts, event emission, and error classification. Concrete adapters implement the three hooks.

### Capability-required extension for `ModelBinding`

An agent that enables semantic caching declares an `embedder_binding` in its `SemanticCacheConfig`. The capability-required compile-time check is extended:

- The chosen embedder must exist in the registry.
- The embedder's dimensions must match the semantic cache backend's expected dimensions (or backend is dynamic-dim).
- `supports_query_document_split` required only if the agent's config requests asymmetric use; otherwise optional.

Failures surface as `EmbedderConfigError` at compile time — same pattern as provider capabilities.

### Rate limiting and cost

Embedders share the `RateLimiter` from the generation side — keyed on `(embedder_provider, embedder_model)` separately from generation keys. Cost estimation via `EmbedderCapabilities.pricing` (typically $/1M input tokens; embedders don't have output tokens).

### Error translation

Mirrors the generation-side error hierarchy:

- `EmbedderAuthError` — 401/403.
- `EmbedderConfigError` — unsupported model, invalid input size, dimension mismatch.
- `EmbedderTimeoutError` — client-side or provider-side timeout.
- `EmbedderUnexpectedError` — anything else, with provider error text in `context`.

## Streaming contract

`stream()` returns an `AsyncIterator[ModelDelta]`. Consumers:

- The CLI / API layer when running in streaming mode — deltas are re-encoded as SSE and sent to the caller.
- `generate()`'s default implementation in the base class — accumulates deltas into a `ModelResponse`.

`ModelDelta` is always provider-agnostic. Provider-specific chunk shapes never leak past the adapter. See `core/model.py` for the `ModelDelta` type.

### Delta assembly rules

- A streaming session emits 1..n deltas, ending with a delta that has `stop_reason != None`.
- `usage` populates on the final delta (and on any delta the provider chooses to send it on — adapters tolerate duplicates by last-wins).
- `content_block_index` addresses which block in the final message the delta belongs to. Adapters maintain per-index state while streaming.
- Tool-use deltas carry partial JSON for the input; the adapter accumulates and parses only on block-close. Consumers should not parse partial JSON.

## Credentials and secrets

Provider credentials never appear in YAML. `ModelBinding.credentials_ref` is the only way to point at a credential:

```python
class CredentialsRef(BaseModel):
    kind: Literal["env", "aws_profile", "secret_manager", "default"]
    value: str | None = None

    # Examples:
    # CredentialsRef(kind="env", value="ANTHROPIC_API_KEY")
    # CredentialsRef(kind="aws_profile", value="bedrock-prod")
    # CredentialsRef(kind="secret_manager", value="gcp/projects/.../secrets/...")
    # CredentialsRef(kind="default")   # use provider SDK's default chain
```

The `SecretsProvider` interface in `foundry.config.secrets` resolves these refs. Providers never read environment variables directly — they ask the `SecretsProvider` threaded in at startup. This keeps secrets discovery testable and allows pluggable backends (Vault, AWS Secrets Manager, GCP Secret Manager).

## Cost estimation

Every `ModelResponse` carries `cost_estimate_usd: Decimal | None`. Computed by `pricing.py`:

```python
def estimate_cost(
    capabilities: ProviderCapabilities,
    usage: TokenUsage,
) -> Decimal:
    p = capabilities.pricing
    return (
        Decimal(usage.input_tokens) * p.input_per_1m / Decimal(1_000_000)
        + Decimal(usage.output_tokens) * p.output_per_1m / Decimal(1_000_000)
        + Decimal(usage.cached_read_tokens) * p.cache_read_per_1m / Decimal(1_000_000)
        + Decimal(usage.cached_write_tokens) * p.cache_write_per_1m / Decimal(1_000_000)
    )
```

Pricing is indicative, not authoritative. Providers bill what they bill; this number is for foundry-internal budgeting (meta-agent cost caps, dev-time monitoring). If pricing is missing for a model, cost is `None` and a warning is logged at startup.

## Error translation

Every provider adapter's `_classify_error()` takes a provider-native exception and returns a `ProviderError` subclass. Rules:

- **Retry classification** lives here too. Each `ProviderError` subclass has a class attr `retryable: bool`. `ProviderRateLimitError.retryable = True`, `ProviderAuthError.retryable = False`, etc. The `ProviderAdapter` base uses this in its retry loop.
- **Context preservation** — original provider error message goes into `context["provider_message"]`; provider-native error code (if any) into `context["provider_error_code"]`; HTTP status if applicable into `context["http_status"]`.
- **No leakage**. The `anthropic.BadRequestError` type is never raised outside `providers.anthropic.*`.

## Rate limiting

Provider rate limits are organisation-level, not per-process. A batch running on 8 workers can trip Anthropic's org rate limit from any single worker. Retrying on 429 is necessary but not sufficient — at scale, blind retries turn into thundering herds.

The `ProviderAdapter` base wraps every `generate`/`stream` call with a pluggable `RateLimiter`:

```python
class RateLimiter(Protocol):
    async def acquire(self, key: str, cost: int = 1) -> None:
        """Block until a permit is available. Honours session cancellation.
        `key` is typically 'anthropic:claude-opus-4-7' — limits are usually
        per-(provider, model). `cost` is the expected token count for
        token-bucket limiters that budget by tokens, not calls."""

    async def report(
        self,
        key: str,
        actual_cost: int,
        was_throttled: bool,
    ) -> None:
        """Feedback after the call. Token-bucket limiters can correct
        based on actual usage; adaptive limiters can tighten on 429s."""
```

### Built-in implementations

| Class | Backing | When to use |
|---|---|---|
| `InProcessTokenBucket` | `anyio.Lock` + monotonic time | Single-worker dev |
| `RedisTokenBucket` | Redis `INCRBY` + EXPIRE | Multi-worker, multi-host prod |
| `AdaptiveRateLimiter` | Wraps either; tightens on observed 429s | Adversarial APIs with opaque headroom |
| `NoOpLimiter` | — | Test fixtures |

Default in v1: `InProcessTokenBucket`. Production deployments set `FOUNDRY_RATE_LIMITER=redis://...` to swap to the Redis-backed limiter without code changes. Configured per-(provider, model) with rates loaded from the per-provider manifest (or overridden via `~/.foundry/rate_limits.yaml`).

### Interaction with retries

The adapter's retry loop respects rate-limiter acquisition. A classified `ProviderRateLimitError` triggers:
1. Rate limiter `report(was_throttled=True)` so the limiter can tighten.
2. Backoff per `RetryPolicy`.
3. Re-acquire a permit before the next attempt (may block longer if the limiter tightened).
4. Retry.

### Circuit breakers

The adapter also wraps a per-(provider, model) `CircuitBreaker`. On sustained failure rates above threshold, the breaker opens for a cool-down window and subsequent calls fast-fail with `ProviderUnexpectedError(context={"circuit": "open"})` rather than hammering a downed dependency. Default: off. Enabled via `ProviderCapabilities.circuit_breaker` in the manifest with `threshold`, `window_s`, `cool_down_s`.

Full wire specification (Redis key layout, failure thresholds, multi-region considerations) in `85-batch-and-throughput.md`.

## Cost-budget integration

Every `ProviderAdapter.generate()` and `.stream()` call participates in `Session.cost_budget` enforcement when a budget is configured:

1. **Pre-call**: estimate the call's cost from `input_tokens × input_per_1m + max_output_tokens × output_per_1m` (a generous upper bound), then call `session.cost_budget.check(estimate)`. Raises `CostBudgetExceeded` if the next call would breach.
2. **Post-call**: compute actual cost from the response's `TokenUsage` × `ModelPricing`, then call `session.cost_budget.record(actual)`.

If `session.cost_budget is None` (no budget set), both calls are no-ops. Embedder and Reranker adapters follow the same pattern with their own pricing.

`CostBudgetExceeded` propagates as a clean `OrchestrationError` subclass and surfaces in the run's terminal `RunFailed` event with the budget's `context` dict — the audit trail records exactly how close the run was to the cap and where it tripped.

## Retry policy

```python
class RetryPolicy(BaseModel):
    max_attempts: int = 3
    backoff: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    initial_delay_s: float = 1.0
    max_delay_s: float = 30.0
    retryable_errors: list[str] = [
        "ProviderRateLimitError",
        "ProviderTimeoutError",
    ]
    jitter: bool = True
```

Default policy is applied to every `generate`/`stream` call. Override via `ModelBinding.settings.retry_policy` (not yet on `ModelSettings` — proposed for Phase 1).

Retry happens inside the adapter, wraps `_build_chat_model().ainvoke(...)`, and honours `anyio.fail_after` on each attempt. Timeouts are per-attempt, not across all attempts — this is intentional (users want a single slow call to be retried, not count against a global budget).

## `ProviderAdapter` base

```python
class ProviderAdapter(ABC):
    name: ClassVar[str]
    capabilities: ProviderCapabilities

    def __init__(
        self,
        model: str,
        settings: ModelSettings,
        credentials: ResolvedCredentials,
        manifest: ProviderCapabilities,
    ) -> None:
        self.model = model
        self.capabilities = manifest
        self._settings = settings
        self._credentials = credentials
        self._chat_model = self._build_chat_model()

    @abstractmethod
    def _build_chat_model(self) -> BaseChatModel: ...

    @abstractmethod
    def _to_provider_messages(
        self, messages: list[FoundryMessage]
    ) -> list[BaseMessage]: ...

    @abstractmethod
    def _from_provider_response(
        self, response: AIMessage, usage_dict: dict, latency_ms: int
    ) -> ModelResponse: ...

    @abstractmethod
    async def _stream_deltas(
        self, stream: AsyncIterator[BaseMessageChunk]
    ) -> AsyncIterator[ModelDelta]: ...

    @abstractmethod
    def _classify_error(self, exc: Exception) -> ProviderError | None:
        """Return classified error, or None to bubble unchanged."""

    async def generate(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema],
        settings: ResolvedModelSettings,
    ) -> ModelResponse:
        """Default impl: accumulate from stream."""
        ...

    async def stream(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema],
        settings: ResolvedModelSettings,
    ) -> AsyncIterator[ModelDelta]:
        """Implements retries + timeouts + error translation around
        _stream_deltas."""
        ...
```

The `_build_chat_model` hook is the ONLY place that references `langchain_*` types. Everything else works with foundry-native types or provider-native types within the adapter.

## Registry and factory

```python
# _registry.py
_ADAPTERS: dict[str, type[ProviderAdapter]] = {}

def register_provider(name: str) -> Callable[[type[ProviderAdapter]], type[ProviderAdapter]]:
    def _decorator(cls):
        if name in _ADAPTERS:
            raise RuntimeError(f"Provider already registered: {name}")
        _ADAPTERS[name] = cls
        return cls
    return _decorator

def resolve(
    binding: ModelBinding,
    secrets: SecretsProvider,
) -> ProviderAdapter:
    try:
        cls = _ADAPTERS[binding.provider]
    except KeyError:
        raise ProviderConfigError(
            f"Unknown provider: {binding.provider!r}",
            context={"available": sorted(_ADAPTERS.keys())},
        )
    manifest = load_capabilities(binding.provider, binding.model)
    creds = secrets.resolve(binding.credentials_ref)
    return cls(
        model=binding.model,
        settings=binding.settings,
        credentials=creds,
        manifest=manifest,
    )
```

Registration happens at import time in each concrete provider module; `providers.__init__` imports each concrete module so the registry is populated at foundry startup.

## Interaction with LangGraph

The orchestration layer calls `resolve(binding, secrets) → ProviderAdapter` once per agent at compile time. The resulting adapter is held by the compiled agent node. When the node runs, it calls `adapter.generate(...)` or `adapter.stream(...)`.

LangGraph's `init_chat_model` is used internally by `_build_chat_model` for most adapters because it handles a lot of provider plumbing we don't want to rewrite (auth, retries, streaming adapters). The key discipline: LangGraph's model object stays *inside* the adapter. Foundry-native types come in, foundry-native types go out.

## Invariants

1. **No `langchain_*` / `anthropic` / `openai` types cross the `foundry.providers` public boundary.** Enforced by import-boundary lint.
2. **Every `Provider` is immutable after construction** — settings are resolved at construct time; changes mean constructing a new adapter.
3. **Capability-required declarations are honoured at compile time, not at runtime.** A binding whose declared capabilities aren't supported must not load.
4. **Every exception raised by `generate()` / `stream()` is a `ProviderError` subclass.** Third-party exceptions are wrapped or re-raised as the generic `ProviderUnexpectedError`.
5. **`cost_estimate_usd` is always populated when `pricing` exists for the model.** Missing pricing → `None` + one startup warning per model per process.
6. **Streaming deltas are self-describing** — consumers do not need to know the provider to assemble a `ModelResponse`.
7. **Retries preserve cancellation.** Inside a retry loop, `session.cancel_token.wait_cancelled()` wins over a backoff sleep.

## Failure modes

| Cause | Surfaced as |
|---|---|
| Unknown provider name | `ProviderConfigError` at `resolve()` |
| Unknown model for provider | `ProviderConfigError` with manifest miss |
| Declared capability not supported | `ProviderConfigError` at compile |
| Credential ref unresolvable | `ProviderAuthError` at construct |
| 401/403 from provider | `ProviderAuthError` at call |
| 429 from provider (after retries exhausted) | `ProviderRateLimitError` |
| Timeout (client-side or provider-side) | `ProviderTimeoutError` |
| Content-policy refusal | `ProviderContentPolicyError` |
| Everything else | `ProviderUnexpectedError` (context carries provider error text) |

## Test expectations

### Unit

1. **Capability-required matrix.** For each concrete provider, assert the manifest's claimed capabilities match the adapter's actual kwargs translation. Drift test: a changed manifest without adapter support fails this test.
2. **Message round-trip.** For each concrete provider, assert `_to_provider_messages(_from_provider_response(...))` preserves structure (via a recorded response fixture).
3. **Error classification.** For each concrete provider, assert each known provider-native exception type maps to the expected `ProviderError` subclass.
4. **Cost estimation.** Given a known `TokenUsage`, assert the cost calculation matches a hand-computed value to 6 decimal places.
5. **Retry loop.** Use a fake adapter whose `_stream_deltas` raises `ProviderRateLimitError` N times then succeeds; assert the wrapper retries and succeeds within `max_attempts`.
6. **Registry.** `resolve({provider: unknown})` raises `ProviderConfigError` with `context['available']` listing known providers.

### Contract

1. **No third-party leak.** `grep -rn 'import langchain\|import anthropic\|import openai' src/foundry/` returns zero hits outside `foundry/providers/*` and `foundry/runtime/langgraph_adapter.py`.
2. **Streaming parity.** For each provider that supports streaming, running `generate()` and `stream()` + accumulate on the same input produce equal `ModelResponse`s (modulo `latency_ms` tolerance).

### Integration (Phase 1 exit gate)

1. **Cross-provider swap.** `foundry run hello --provider anthropic` vs `foundry run hello --provider openai` both succeed on the same agent config (only `model_binding.provider + model` differ).
2. **Capability failure.** Config declaring `cache_control: true` with `provider: openai` fails to load with a clear error naming the failing capability.

## Implementation notes (non-normative)

- **`init_chat_model` bridge.** Wrap once in `_langchain_bridge.py`. Do not scatter `init_chat_model` calls through per-provider modules.
- **Keep `_classify_error` simple.** Prefer exception-type identity checks (`isinstance(exc, anthropic.RateLimitError)`) over string matching. HTTP status codes and error codes are a fallback.
- **Pricing manifests are user-extensible.** Ship sensible defaults; document the override file at `~/.foundry/model_overrides.json`. Meta-agent does not modify pricing files.
- **Reasoning models' output handling.** OpenAI o-series models return `reasoning_tokens` in usage; Anthropic extended thinking returns thinking blocks in content. Adapters normalise both into `TokenUsage` fields (adding `reasoning_tokens` if needed — see open question 2).
- **Timeout hygiene.** `anyio.fail_after` around each attempt, NOT around the retry loop. Each attempt gets its own budget.
- **Observability.** The adapter base class emits the `foundry.llm` span with all attributes listed in the observability attribute spec (see `01-architecture-overview.md` § Observability summary).

## Open questions

1. **Do we add `reasoning_tokens` to `TokenUsage`?** OpenAI o-series and future Anthropic models may surface it distinctly from `output_tokens`. Lean: yes, add `reasoning_tokens: int = 0` to `TokenUsage` now so we don't break downstream consumers when it becomes common.
2. **Do we expose `cache_control` at the `TextBlock` level or only as a `ModelSettings.cache_control: CacheControlMode`?** Current design: both. The `CacheControlMode` governs which blocks get marked automatically; explicit per-block `cache_control` is for advanced users who want surgical control.
3. **Do we support a `ModelRouter` pseudo-provider?** I.e. "use anthropic when X, openai when Y." Lean: no for v1; introduces cost and complexity; can be done by the orchestration layer's routing pattern instead.
4. **`provider_overrides` sandbox.** RESOLVED 2026-04-25: meta-agent MUST NOT populate `provider_overrides`. Enforced via the meta-agent's prompt (explicit forbid) AND by the `build_tool` / `build_agent` scaffolds not surfacing it as a knob. Humans edit YAML manually if the escape hatch is genuinely needed. To be enforced in `60-meta-agent.md` prompt + `61-meta-tools.md` scaffold spec.
5. **Vertex deferral.** Implement in Phase 1 or push to Phase 9? Recommend Phase 1 skeleton with manifests and a stub; full implementation as a Phase 1 polish task if time permits, else Phase 9.
