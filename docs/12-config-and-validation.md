# 12 — Config and Validation

## Purpose

Every artifact in the foundry is authored as a text file — YAML for configs, markdown for prompts, Python for tool handlers and output schemas. This layer is where those text files become typed Pydantic objects that the rest of the foundry programs against. Two load-bearing properties:

1. **No untyped dicts leak past the loader.** Everything downstream works with Pydantic instances. If a field is named wrong in YAML, the user finds out at load time with a precise error message, not at runtime when something blows up three function calls later.
2. **The Pydantic schemas ARE the spec.** This doc enumerates them. A field added or renamed here is an API change. The meta-agent reads, generates, and edits YAML that must conform to these schemas.

## Module layout

```
src/foundry/config/
├── __init__.py         public API: load_* helpers + schema re-exports
├── loader.py           YAML → Pydantic; structured error reporting
├── schemas.py          the Pydantic models this doc enumerates
├── composition.py      includes, overrides, env interpolation
├── refs.py             ArtifactRef parsing + validation (catalog vs local)
├── secrets.py          SecretsProvider interface + built-in resolvers
└── jsonschema.py       Pydantic → JSON Schema emission for IDE hints
```

## What `config` imports

- `foundry.core` (for types).
- `pydantic`, `pyyaml`, `anyio`, stdlib.
- No other foundry module. Providers, orchestration, eval etc. consume config — not the other way around.

## File → schema map

| File | Parser entrypoint | Schema |
|---|---|---|
| `projects/<name>/system.yaml` | `load_system_spec(path)` | `SystemSpec` |
| `projects/<name>/state.yaml` | `load_state_spec(path)` | `StateSpec` |
| `projects/<name>/agents/<agent>/agent.yaml` | `load_agent_spec(path)` | `AgentSpec` |
| `projects/<name>/tools/<tool>/v<N>/tool.yaml` | `load_tool_spec(path)` | `ToolSpec` |
| `catalog/tools/<tool>/v<N>/tool.yaml` | `load_tool_spec(path)` | `ToolSpec` (same) |
| `projects/<name>/connections/<name>/v<N>/connection.yaml` | `load_connection_spec(path)` | `ConnectionSpec` |
| `catalog/connections/<name>/v<N>/connection.yaml` | `load_connection_spec(path)` | `ConnectionSpec` (same) |
| `projects/<name>/evals/<name>.yaml` | `load_eval_spec(path)` | `EvalSpec` |
| `catalog/tools/<tool>/versions.json` | `load_versions_metadata(path)` | `VersionsMetadata` |
| `catalog/connections/<name>/versions.json` | `load_versions_metadata(path)` | `VersionsMetadata` |
| `catalog/index.yaml` | `load_catalog_index(path)` | `CatalogIndex` |

## The loader

```python
def load_system_spec(path: Path) -> SystemSpec: ...
def load_state_spec(path: Path) -> StateSpec: ...
def load_agent_spec(path: Path) -> AgentSpec: ...
def load_tool_spec(path: Path) -> ToolSpec: ...
def load_connection_spec(path: Path) -> ConnectionSpec: ...
def load_eval_spec(path: Path) -> EvalSpec: ...
```

All follow the same pipeline:

```
text file
  → pyyaml.safe_load (no !python constructors, no !include directives at this level)
  → composition pass (§ Composition)
  → env interpolation (§ Env interpolation)
  → secret-literal scan (§ Secrets)
  → Pydantic model validation
  → typed instance returned
  (errors at any stage → ConfigLoadError or ConfigValidationError with full context)
```

### Error reporting

Every loader error carries:
- `file`: absolute path of the file being loaded
- `pointer`: JSON-pointer-like path into the YAML structure (e.g. `/agents/0/model_binding/provider`)
- `line` / `column`: when pyyaml can tell us (attached via `SafeLoader` + `composer.CustomSafeLoader` that preserves node positions)
- `message`: human message
- `received` / `expected`: when Pydantic validation fails

Example:

```
ConfigValidationError: Invalid SystemSpec
  file: projects/pipeline_recon/system.yaml
  pointer: /flow/handoff_policy/mode
  line: 18, column: 9
  received: "llb"
  expected: one of ["llm", "rule", "hybrid"]
  hint: did you mean "llm"?
```

The "hint" is produced by a best-effort Levenshtein match against enum values or field names. Optional but valuable.

## Top-level schemas

### `SystemSpec`

The project manifest. One per project.

```python
class SystemSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    description: str
    agents: list[str] = Field(min_length=1)
    """Agent names (not refs) — relative to projects/<name>/agents/*."""

    state: str = "state.yaml"
    """Relative path to the state spec."""

    flow: FlowSpec
    """Orchestration config — see 30-orchestration-patterns.md."""

    tools: dict[str, ToolBinding] = Field(default_factory=dict)
    """Map of tool logical-name → version-pinned binding. These are
    the tools available to agents via their allowlist."""

    connections: dict[str, ConnectionBinding] = Field(default_factory=dict)
    """Map of connection logical-name (as referenced by tool slots) →
    version-pinned binding. Each entry pins a specific ConnectionSpec
    version and supplies the config + credentials_ref needed to build it.
    Tool allowlisting happens at the agent level; connection binding
    happens at the project level because connections are cross-cutting."""

    guardrails: Guardrails = Field(default_factory=Guardrails)
    observability: ObservabilityConfig = Field(default_factory=ObservabilityConfig)
    metadata: dict[str, Any] = Field(default_factory=dict)
    """Free-form user metadata; not validated beyond being JSON-compatible."""

    schema_version: Literal[1] = 1
    """Schema version for forward-compat."""
```

### `ToolBinding`

A version-pinned tool reference that sits inside `SystemSpec.tools`:

```python
class ToolBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(pattern=r"^(catalog|local)/[a-z][a-z0-9_-]{0,63}$")
    """e.g. 'catalog/query_snowflake' or 'local/validate_deltas'."""

    version: str = Field(pattern=r"^v\d+$")
    """The pinned version directory name."""

    settings: dict[str, Any] = Field(default_factory=dict)
    """Optional per-project tool overrides (timeout_s, retry_policy).
    Not all tool settings are overridable; validated against the tool's
    declared overridable_settings list at compile."""

    connection_bindings: dict[str, str] = Field(default_factory=dict)
    """Maps the tool's declared connection slots → connection logical-name
    in SystemSpec.connections. Compile-time check: every slot the tool
    lists in connections_required MUST appear here, and every name must
    resolve to a ConnectionBinding in SystemSpec.connections."""
```

### `FlowSpec`

A discriminated union over pattern types:

```python
class SequentialFlow(BaseModel):
    type: Literal["sequential"] = "sequential"
    steps: list[str] = Field(min_length=1)

class ParallelFlow(BaseModel):
    type: Literal["parallel"] = "parallel"
    parallel_branches: list[str] = Field(min_length=2)
    join: str | None = None
    then: list[str] = Field(default_factory=list)

class SingleFlow(BaseModel):
    type: Literal["single"] = "single"
    agent: str

class SupervisorFlow(BaseModel):
    type: Literal["supervisor"] = "supervisor"
    supervisor: str
    workers: list[str] = Field(min_length=1)
    handoff_policy: HandoffPolicy = Field(default_factory=HandoffPolicy)
    termination: TerminationRule = Field(default_factory=TerminationRule)

class GraphFlow(BaseModel):
    type: Literal["graph"] = "graph"
    start: str
    edges: list[GraphEdge] = Field(min_length=1)

FlowSpec = Annotated[
    SingleFlow | SequentialFlow | ParallelFlow | SupervisorFlow | GraphFlow,
    Field(discriminator="type"),
]
```

`HandoffPolicy`, `TerminationRule`, `GraphEdge` and friends live here too but their full spec is in `30-orchestration-patterns.md`. The shape is given there; this doc is about how they're validated at load.

### `Guardrails`

```python
class Guardrails(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_iterations: int = Field(default=10, ge=1, le=1000)
    """Max LLM calls before the orchestration layer aborts."""

    max_hops: int = Field(default=20, ge=1, le=1000)
    """Max agent hops (handoffs) before abort."""

    max_cost_usd: Decimal | None = None
    """Hard budget cap for a run. None = no cap."""

    max_wall_time_s: float | None = None
    """Per-run wall clock limit."""
```

### `ObservabilityConfig`

```python
class ObservabilityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    trace: Literal["otel", "langsmith", "off"] = "otel"
    sample_rate: float = Field(default=1.0, ge=0.0, le=1.0)
    capture_inputs: bool = True
    capture_outputs: bool = True
    capture_tool_args: bool = True
    """Set to False to exclude tool inputs/outputs from the run artifact for
    PII-sensitive projects. Dimensions (latencies, costs) are still captured."""
```

### `AgentSpec`

```python
class AgentSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    description: str = ""

    model_binding: ModelBinding

    prompt: PromptRef
    output: OutputSchemaRef

    tools: list[str] = Field(default_factory=list)
    """Names that must exist in the parent SystemSpec.tools map.
    Compile error if an agent names a tool not bound at system level."""

    state_visibility: StateVisibility
    """Per-agent read/write access to state fields. Enforced at compile."""

    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    """Default retry policy for this agent's LLM calls."""

    iteration_limit: int = Field(default=20, ge=1, le=500)
    """Max tool-call rounds for this agent in a single invocation."""

    metadata: dict[str, Any] = Field(default_factory=dict)
    schema_version: Literal[1] = 1

class PromptRef(BaseModel):
    version: str = Field(pattern=r"^v\d+$")
    path: str
    """Relative to the agent directory, e.g. 'prompts/v3.md'."""

    @model_validator(mode="after")
    def _check_consistency(self) -> "PromptRef":
        # path must end with "<version>.md"
        if not self.path.endswith(f"{self.version}.md"):
            raise ValueError(f"path {self.path!r} does not match version {self.version!r}")
        return self

class OutputSchemaRef(BaseModel):
    schema: str
    """Module:Class path, e.g. 'output_schema.py::Greeting'. Loaded by importlib
    at compile time relative to the agent directory."""
```

### `StateSpec`

```python
class StateSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema: dict[str, FieldSpec]
    """Map of field-name → type descriptor. Drives Pydantic schema generation."""

    reducers: dict[str, Reducer] = Field(default_factory=dict)
    """Optional per-field reducer overrides. Unlisted fields default to
    LAST_WRITE_WINS."""

    visibility: dict[str, StateVisibility]
    """Per-agent visibility. Keys are agent names; must cover every agent in
    SystemSpec.agents (compile-time check)."""

    schema_version: Literal[1] = 1

class FieldSpec(BaseModel):
    type: str
    """Type string. Supported: primitives (str, int, float, bool, datetime,
    date, Decimal), List/Dict/Optional wrappers, FoundryMessage,
    'BaseModel:<module>:<class>' for user Pydantic types.
    Parsed by state.py into runtime Python types."""

    default: Any | None = None
    description: str = ""

class StateVisibility(BaseModel):
    read: list[str]
    write: list[str]

    @model_validator(mode="after")
    def _no_nameless(self) -> "StateVisibility":
        if not self.read and not self.write:
            raise ValueError("agent must declare at least one of read or write")
        return self
```

### `ToolSpec`

The shape of each `tool.yaml` inside a versioned tool directory (`tools/<name>/v<N>/tool.yaml`):

```python
class ToolSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^v\d+$")
    description: str

    input_schema: str
    output_schema: str
    """Both are 'schemas.py::ClassName' refs, relative to the tool's version directory."""

    handler: str
    """'handler.py::function_name', relative to the tool's version directory.
    Loaded and wrapped by ToolRegistry at compile."""

    timeout_s: float = Field(default=30.0, gt=0)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    overridable_settings: list[str] = Field(default_factory=lambda: ["timeout_s", "retry_policy"])
    """Which settings the consuming project may override via ToolBinding.settings."""

    tags: list[str] = Field(default_factory=list)
    """Free-form search/discovery tags. Used by meta-agent's list_tools."""

    standalone_eval: str | None = "eval.yaml"
    """Path to the tool's standalone EvalSpec, relative to this directory.
    None means no standalone eval (discouraged but allowed)."""

    connections_required: list[ConnectionSlot] = Field(default_factory=list)
    """Connection slots this tool needs. Each entry declares the slot name
    the handler will use (`ctx.connections.get(slot)`) and which connection
    refs are acceptable for that slot."""

    author: str | None = None
    created_at: datetime | None = None
    schema_version: Literal[1] = 1

class ConnectionSlot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slot: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    """Name the tool handler uses to request this connection."""

    accepts: list[str] = Field(min_length=1)
    """List of connection ref prefixes this slot accepts.
    Examples:
        accepts: [catalog/snowflake]              # any version
        accepts: [catalog/snowflake, catalog/bigquery]
        accepts: [catalog/postgres@v1]            # exact version pin
    Compile-time check: the slot's bound connection ref must match at least
    one prefix here."""

    description: str = ""
    optional: bool = False
    """If True, the tool handler will receive None when no connection is
    bound to this slot. Defaults False — most slots are required."""
```

### `ConnectionSpec`

The shape of each `connection.yaml` inside a versioned connection directory:

```python
class ConnectionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^v\d+$")
    description: str

    auth_scheme: AuthScheme
    """Which auth strategy this connection implements. Enum from core."""

    config_schema: str
    """'schemas.py::ClassName' — Pydantic model describing non-secret
    connection settings (e.g. Snowflake account/warehouse/role)."""

    factory: str
    """'auth.py::build_connection' — async callable conforming to the
    ConnectionFactory protocol. Loaded by ConnectionPool at compile."""

    client_type: str = ""
    """Human-readable description of the client object the factory returns,
    for docs and meta-agent discovery. e.g. 'snowflake.connector.SnowflakeConnection'."""

    health_check: str | None = "health.yaml"
    """Path to an EvalSpec that checks the connection can issue a trivial
    query against the real system. Used by `foundry connections health`
    and optionally by Phase 9 startup health-probes."""

    refresh: RefreshPolicy = Field(default_factory=RefreshPolicy)
    pool: PoolPolicy = Field(default_factory=PoolPolicy)

    non_sensitive_config_fields: list[str] = Field(default_factory=list)
    """Allowlist of config-schema field names safe to include in
    ConnectionDescriptor.redacted_config. Anything not listed is dropped
    by the redactor. Deliberately opt-in."""

    tags: list[str] = Field(default_factory=list)
    """Discovery tags for meta-agent (`list_connections` filtering)."""

    author: str | None = None
    created_at: datetime | None = None
    schema_version: Literal[1] = 1

class RefreshPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["none", "expiry", "periodic", "on_auth_error"] = "expiry"
    """none: build once, never refresh.
       expiry: use token/cert expiry info when available.
       periodic: refresh every refresh_interval_s regardless.
       on_auth_error: evict and rebuild when a ConnectionAuthError is raised
                      during use."""

    refresh_interval_s: int | None = None
    early_refresh_buffer_s: int = 60
    """How many seconds before computed expiry to pre-emptively refresh."""

class PoolPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_concurrent: int = Field(default=32, ge=1, le=1024)
    """Concurrent checkout ceiling per pool entry."""

    idle_ttl_s: int | None = None
    """Evict idle entries after this long. None = never."""

    acquire_timeout_s: float = 30.0

class AuthScheme(StrEnum):
    API_KEY = "api_key"
    BASIC_AUTH = "basic_auth"
    OAUTH2_CLIENT_CREDENTIALS = "oauth2_client_credentials"
    OAUTH2_REFRESH_TOKEN = "oauth2_refresh_token"
    JWT_BEARER = "jwt_bearer"
    SIGV4 = "sigv4"
    MTLS = "mtls"
    CUSTOM = "custom"
```

### `ConnectionBinding`

Sits in `SystemSpec.connections`. Pins a specific connection version and supplies the per-project config + credentials. This is where secrets enter (via `credentials_ref`) — never in `ConnectionSpec` itself, which ships with the connection and is reusable across environments.

```python
class ConnectionBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str = Field(pattern=r"^(catalog|local)/[a-z][a-z0-9_-]{0,63}$")
    """e.g. 'catalog/snowflake', 'local/internal_trading_system'."""

    version: str = Field(pattern=r"^v\d+$")

    config: dict[str, Any] = Field(default_factory=dict)
    """Instance of the ConnectionSpec's config_schema. Validated against it
    at compile time. Non-secret fields only; secrets go in credentials_ref."""

    credentials_ref: CredentialsRef
    """REQUIRED. All connections authenticate; the credentials source is
    always explicit. Use kind='default' only for connections whose factory
    uses an SDK default chain (e.g. aws_session relying on boto3's chain)."""

    refresh_overrides: RefreshPolicy | None = None
    """Optional per-project override of the ConnectionSpec's refresh policy."""

    pool_overrides: PoolPolicy | None = None
    """Optional per-project override of pool sizing."""

    metadata: dict[str, Any] = Field(default_factory=dict)
```

The secret-literal scan in the loader (see § Secrets) runs across `config` and rejects any value that looks like a credential, since this is exactly the field where a well-meaning copy-paste would land.

### `EvalSpec`

```python
class EvalSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str = ""
    scope: Literal["tool", "agent", "project"]
    """What this eval runs against. Determines loading and execution path."""

    target: str
    """A ref: 'catalog/query_snowflake@v2' for tool; agent name for agent;
    project name for project. Resolved at load time."""

    cases: list[EvalCase] = Field(min_length=1)
    scorers: list[ScorerConfig] = Field(min_length=1)

    threshold: float = Field(default=0.9, ge=0.0, le=1.0)
    """Pass/fail threshold on the aggregated score."""

    max_parallel: int = Field(default=4, ge=1, le=64)
    deterministic: bool = True
    """When True, eval sets a seed and asserts providers honour it where
    they can. When False, results may vary and are reported as a distribution."""

    seed: int | None = None
    schema_version: Literal[1] = 1

class EvalCase(BaseModel):
    id: str
    input: dict[str, Any]
    expected: Any
    """Expected value shape depends on scorers: exact wants primitive equality,
    llm_judge wants a rubric, rubric wants a dict of rubric keys."""
    tags: list[str] = Field(default_factory=list)
    weight: float = Field(default=1.0, ge=0.0)

class ScorerConfig(BaseModel):
    kind: Literal["exact", "llm_judge", "rubric", "user"]
    name: str
    """Used in reports and to disambiguate multiple scorers of same kind."""
    config: dict[str, Any] = Field(default_factory=dict)
    weight: float = 1.0
    """Scorer weight in aggregated score."""
```

### `CatalogIndex` and `VersionsMetadata`

```python
class CatalogIndex(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    tools: list[str] = Field(default_factory=list)
    """List of tool directory names under catalog/tools/."""
    agent_templates: list[str] = Field(default_factory=list)
    """List under catalog/agent_templates/. Optional feature."""

class VersionsMetadata(BaseModel):
    """Metadata file at tools/<name>/versions.json (catalog or local)."""
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1] = 1
    versions: list[VersionMetadata]

class VersionMetadata(BaseModel):
    version: str = Field(pattern=r"^v\d+$")
    created_at: datetime
    created_by: Literal["human", "meta_agent"]
    eval_score: float | None = None
    eval_run_id: str | None = None
    notes: str = ""
    deprecated: bool = False
    deprecation_reason: str | None = None
```

## ArtifactRef parsing

```python
class ArtifactRef(BaseModel):
    scope: Literal["catalog", "local"]
    kind: Literal["tool", "agent_template"]
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^v\d+$")

    @classmethod
    def parse(cls, s: str) -> "ArtifactRef":
        """Parses 'catalog/query_snowflake@v2' or
        'local/validate_deltas@v3' or
        'catalog/agent_templates/summarizer@v1'."""
        ...

    def to_str(self) -> str:
        prefix = f"{self.scope}"
        if self.kind == "agent_template":
            prefix += "/agent_templates"
        return f"{prefix}/{self.name}@{self.version}"

    def resolve_path(self, roots: FoundryRoots) -> Path: ...
```

`FoundryRoots` is the small struct that tells the resolver where `catalog/` and `projects/<name>/` live on disk:

```python
class FoundryRoots(BaseModel):
    repo_root: Path
    catalog_root: Path
    projects_root: Path
    project_name: str | None = None
    """If set, 'local/...' refs resolve against projects_root/project_name."""
```

A ref that references a non-existent version or path fails fast at `resolve_path` with `RefResolutionError` including the resolved absolute path and a list of available versions if the name exists but the version doesn't.

## Composition

Two features, deliberately small: **`extends`** and **env interpolation**. No recursive includes. No Jinja. The intent is legibility — a config file must be readable top to bottom with at most one `extends` hop.

### `extends`

Any config file MAY start with:

```yaml
extends: ../shared/base-agent.yaml
```

Behaviour: `base-agent.yaml` is loaded first (into a plain dict), then the current file's contents shallow-merge on top. Keys the current file provides override; keys it doesn't touch fall through from the base. List fields are *replaced*, not extended — this is the safer default.

Limits:
- Only one `extends` per file.
- Target must be within the repo (same guardrail as writes).
- `extends` itself is removed before validation — a merged dict is what Pydantic sees.

### Env interpolation

A scalar value of the form `${ENV:NAME}` or `${ENV:NAME:default}` is substituted at load time:

```yaml
observability:
  trace: ${ENV:FOUNDRY_TRACING:otel}
```

Rules:
- Interpolation is scalar-only (no interpolation inside keys or nested structures).
- Only the `ENV:` namespace is supported. No `FILE:`, no arbitrary command substitution.
- Missing env var with no default → `ConfigLoadError`.
- The substituted value is a string; Pydantic coerces to the target type during validation.

### Why so restrictive

Config complexity is the enemy of legibility. A meta-agent that has to understand Jinja to edit YAML is a meta-agent that produces unpredictable edits. Two simple features cover 95% of real cases; anything more exotic should be a code change, not a config trick.

## Secrets

Secrets never appear in YAML. Period.

### Detection heuristic

The loader runs a secret-literal scan over every scalar value before Pydantic validation. Values that match any of:

- Obvious AWS access key pattern (`AKIA[0-9A-Z]{16}`)
- Anthropic key prefix (`sk-ant-`)
- OpenAI key prefix (`sk-`)
- Anything matching a pluggable regex from `~/.foundry/secret_patterns.yaml`
- Anything with a key name containing `password|secret|token|api_key|apikey` and a literal scalar value longer than 8 characters

Trigger a `ConfigLoadError` with a hint:

> "Detected likely secret literal at /model_binding/settings/api_key (projects/hello/agents/hello_agent/agent.yaml:14). Secrets must live in env vars or a secrets provider; use credentials_ref."

This is a false-positive-prone heuristic — that's the point. Noisy is safer than silent when secrets are involved. Users override with `# foundry:allow-literal` pragma comments on the offending line when the heuristic is wrong (rare).

### `SecretsProvider`

```python
class SecretsProvider(Protocol):
    def resolve(self, ref: CredentialsRef | None) -> ResolvedCredentials: ...

class EnvSecretsProvider(SecretsProvider):
    """Default. Resolves kind='env' against os.environ. kind='default'
    means 'let the SDK's default chain handle it'."""

class AWSSecretsManagerProvider(SecretsProvider): ...
class GCPSecretManagerProvider(SecretsProvider): ...
class VaultSecretsProvider(SecretsProvider): ...
```

The foundry process has one `SecretsProvider` at startup. Default is `EnvSecretsProvider`. CLI flag `--secrets-provider aws|gcp|vault|env` swaps. API layer config sets it.

A `ResolvedCredentials` is a typed opaque wrapper — providers read from it; its string form is redacted in logs.

## JSON Schema emission

For IDE hints and external tooling, every top-level schema emits JSON Schema:

```python
def emit_jsonschemas(out_dir: Path) -> None:
    schemas = [SystemSpec, AgentSpec, StateSpec, ToolSpec, EvalSpec, CatalogIndex]
    for S in schemas:
        (out_dir / f"{S.__name__.lower()}.schema.json").write_text(
            json.dumps(S.model_json_schema(), indent=2)
        )
```

Emitted to `docs/_schemas/` as part of the dev-doc build. VS Code `yaml.schemas` entries in the repo's `.vscode/settings.json` point at these, so editing YAML gets completion + validation in-editor.

## Validation strategy

Three phases of validation, each with clear ownership:

1. **Syntactic** — is the YAML valid? Is the shape a dict/list/scalar? Done by pyyaml.
2. **Schema** — do keys/types match a `BaseModel`? Done by Pydantic at load.
3. **Semantic** — do cross-field constraints hold? Done by Pydantic `model_validator`s AND by the orchestration compiler.

Examples of each:

- Syntactic: `agent.yaml` has malformed YAML → `ConfigLoadError("pyyaml failed", line=12)`.
- Schema: `agent.yaml` has `model_binding: "claude"` (a string, not a dict) → `ConfigValidationError` with the pointer.
- Semantic: `SystemSpec.tools` contains `validate_deltas` at `version: v99` but `local/validate_deltas` on disk only has `v1/` and `v2/` → `RefResolutionError` at compile-time ref resolution (not at load).

### Where to put a check

Rule of thumb:

- If the check involves only fields in the same schema → put it in a `model_validator`.
- If it involves the filesystem (file exists, directory has the right shape) → compiler (`foundry.orchestration.compiler`), raised as `CompileError`.
- If it involves cross-schema consistency (agent names in AgentSpec files match those in SystemSpec.agents) → compiler, raised as `CompileError`.

Put checks at the *earliest reliable* point. Never duplicate checks at multiple layers; duplicated checks drift.

## Strict-mode defaults

Every top-level `BaseModel` has `extra="forbid"`. Unknown fields are errors, not warnings. This is deliberate:

- The meta-agent shouldn't be inventing field names; forcing strict keeps it honest.
- Catches typos (`temparature` vs `temperature`) the moment they're saved.
- Future backwards-compat: when a new field is added, `schema_version` bumps; old configs still load because they don't mention the new field.

Opt-out escape hatches where genuinely needed:
- `metadata: dict[str, Any]` fields on `SystemSpec`, `AgentSpec`, etc. — structured-but-unvalidated free-form data.
- `provider_overrides: dict[str, Any]` on `ModelBinding` — sanctioned escape hatch, discouraged.

## Schema evolution

- `schema_version: Literal[1] = 1` on every top-level schema.
- To add a field: default it, don't touch `schema_version`. Old configs still load.
- To rename or remove: bump to `schema_version: Literal[2] = 2`; keep a `v1_to_v2` migration function in `config/migrations.py`. The loader sees `schema_version: 1` and runs the migration before validating.
- Migrations are idempotent, pure (no I/O), and exhaustively tested.
- The meta-agent always writes the current `schema_version`; it does not produce old-version configs.

## Invariants

1. **YAML → Pydantic is the only path to typed objects.** No code constructs `SystemSpec(**raw_dict)` outside the loader.
2. **Pydantic models are the API contract.** A schema change is versioned; downstream consumers pin by `schema_version`.
3. **No secrets in YAML.** Detection heuristic runs on every load. False positives are resolvable via pragma comment; silent passage is not.
4. **Extra fields are forbidden** on all top-level schemas. `ConfigValidationError` lists the offending key and whether it's a typo.
5. **Composition is one-deep.** `extends` is the only compose feature; recursive includes are forbidden.
6. **Env interpolation is scalar-only.** No surprise substitutions in keys or list elements.
7. **ArtifactRef strings round-trip.** `ArtifactRef.parse(ref.to_str()) == ref` for every valid ref.
8. **Loader errors never silently fall back to defaults.** If validation fails, the load fails; nothing downstream sees a partial config.

## Failure modes

| Cause | Surfaced as |
|---|---|
| File not found | `ConfigLoadError` |
| YAML parse error | `ConfigLoadError` with line/column |
| Extra field | `ConfigValidationError` with pointer + hint |
| Wrong type | `ConfigValidationError` with received vs expected |
| Missing required field | `ConfigValidationError` with pointer |
| Regex pattern mismatch (name format, version format) | `ConfigValidationError` with pattern + received |
| Enum mismatch | `ConfigValidationError` with received + enum members + hint |
| Cross-field validator fails | `ConfigValidationError` from the `model_validator` |
| Secret literal detected | `ConfigLoadError` before Pydantic sees it |
| Env var missing | `ConfigLoadError` naming the var |
| `extends` target not found | `ConfigLoadError` with the resolved absolute path |
| `schema_version` unknown | `ConfigLoadError` prompting to upgrade the foundry |

## Test expectations

### Unit

1. **Round-trip every schema.** For each top-level `BaseModel`, hand-author a representative YAML fixture, load it, dump it, re-load it, assert equality.
2. **Typo detection.** For each schema, construct YAML with a misspelled field; assert error message lists the expected field with a Levenshtein-matched hint.
3. **Extra-field rejection.** Same, with an extra field; assert rejection.
4. **Enum hint.** Fixture with a near-match enum value; assert the error hint points at the right one.
5. **`extends` shallow merge.** Base and overlay YAMLs; assert merged result has overlay-provided keys and base-provided untouched keys.
6. **Env interpolation.** Set env var, load fixture, assert substitution; unset var without default, assert clear error.
7. **Secret detection.** For each pattern (AWS, Anthropic, OpenAI, generic key-name heuristic), fixture + assert detection fires.
8. **ArtifactRef parse.** Valid refs parse; invalid refs raise with clear messages.
9. **Cross-field validators.** e.g. `PromptRef.path` must end with `.md` matching `version`; assert.
10. **Migration.** For any schema with a v1→v2 migration, load a v1 fixture, assert it migrates and produces a valid v2.

### Contract

1. **JSON Schema emission.** For each schema, `model_json_schema()` returns a JSON-Schema-valid draft. Run the output through `jsonschema` lib's validator-of-schemas.
2. **No dict leak.** Grep `src/foundry/` outside `config/`: no `yaml.safe_load(` calls. All YAML goes through the loader.

### Integration (Phase 0 / Phase 1 exit gate)

1. **Load a hello-world project.** Construct `projects/hello/` with minimal valid configs; assert all loaders run and produce typed objects.
2. **Every exit-gate assertion about structured errors in Phase 1 passes** — configs with known defects produce the documented error shapes.

## Implementation notes (non-normative)

- **pyyaml with `SafeLoader`.** Never `FullLoader` — we don't want `!python` constructors enabling arbitrary instantiation.
- **Position tracking.** Custom `SafeLoader` subclass that annotates nodes with `(line, column)` — standard trick; there's recipes upstream. Attach to error `context`.
- **JSON-pointer generation.** Convert Pydantic's error `loc` tuples to JSON-pointer strings; pointer is what appears in error messages.
- **Loader performance.** Loading a single project's configs is a handful of small files; no perf hotspot expected. If catalog scanning becomes slow with hundreds of tools, add an `index.yaml` cache.
- **Secret-detection false positives.** Ship `foundry config check <file>` CLI — runs the detection and prints what it found, so users can `# foundry:allow-literal` comment before load-time errors surprise them.
- **Migrations testing.** Keep old-version fixtures in `tests/fixtures/config/v1/` forever. Migrations are forever.

## Open questions

1. **Should `extends` support relative URLs (for cross-project sharing)?** v1 recommendation: no. Local paths only. Cross-project sharing should go through the catalog, not config inheritance.
2. **Schema versioning cadence.** We ship at `schema_version: 1`. Under what criteria do we bump? Recommend: breaking change (removed field, renamed field, changed type of existing field). Additive changes don't bump. Document in `50-versioning-model.md`.
3. **User-pluggable scorers in `ScorerConfig`.** `kind: "user"` + `name: "..."` — how do users register? Proposal: an entry-point in `pyproject.toml`; `foundry.eval.scorers.registry` discovers. Detailed in `40-eval-harness.md`.
4. **YAML vs TOML.** Users who prefer TOML. Lean: no in v1 — one format reduces cognitive load.
5. **`description` required or optional?** Many schemas have `description: str = ""`. For shareable artifacts (catalog tools, catalog agent templates) require non-empty; for project-local / system metadata, allow empty. Decision: add a `min_length=1` to catalog-scope schemas when promoted. Defer to `50-versioning-model.md`.
