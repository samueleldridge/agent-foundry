# 23 — Connections and Auth

## Purpose

This doc specifies the `Connection` subsystem: the standardised, versioned, pooled, audited way that tools talk to external enterprise systems. It is the answer to *"every tool in the catalog needs Snowflake access — where does auth live?"*

Two load-bearing properties:

1. **Tools request slots; projects bind connections; the runtime issues authenticated clients.** Tool authors never write auth code. Project operators never paste credentials into YAML. The runtime handles pooling, refresh, health, and audit.
2. **Auth schemes are swappable without tool changes.** Rolling a Snowflake integration from password auth (`v1`) to key-pair (`v2`) to SSO (`v3`) is a new catalog connection version + a project pin bump. Tools that use it keep running.

Connection primitives (`Connection`, `ConnectionPool`, `ConnectionAccessor`, `ConnectionFactory`, `ConnectionHealth`, `ConnectionDescriptor`, `AuthScheme`) are defined in `10-core-framework.md`. Config schemas (`ConnectionSpec`, `ConnectionBinding`) are in `12-config-and-validation.md`. This doc is the full behavioural spec.

## Mental model

```
   ┌──────────────────────────────────────────────────────────────────────┐
   │                                                                      │
   │  projects/pipeline_recon/system.yaml                                 │
   │    connections:                                                      │
   │      prod_snowflake:                                                 │
   │        ref: catalog/snowflake                                        │
   │        version: v1                                                   │
   │        config: { account: ..., warehouse: ... }                      │
   │        credentials_ref: { kind: secret_manager, value: vault/... }   │
   │                                                                      │
   │    tools:                                                            │
   │      query_snowflake:                                                │
   │        ref: catalog/query_snowflake                                  │
   │        version: v1                                                   │
   │        connection_bindings:                                          │
   │          warehouse: prod_snowflake       ◄── slot    bound name      │
   │                                                                      │
   └──────────────────────────────────────────────────────────────────────┘
                                  │
                         compile-time validation
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │  CompiledSystem                                                      │
   │    wiring: query_snowflake.warehouse → prod_snowflake                │
   │    prod_snowflake = (catalog/snowflake@v1, cfg_hash, creds)          │
   └──────────────────────────────────────────────────────────────────────┘
                                  │
                                  ▼
   ┌──────────────────────────────────────────────────────────────────────┐
   │  At run time, inside query_snowflake.handle(inputs, ctx):            │
   │    client = await ctx.connections.get("warehouse")                   │
   │    # ctx.connections translates "warehouse" → prod_snowflake         │
   │    # ConnectionPool.acquire(ref, cfg_hash, project, factory, creds)  │
   │    # - cache hit → return existing Connection                        │
   │    # - cache miss → factory(config, credentials, ctx) → Connection   │
   │    # returns Connection.client (typed SnowflakeConnection)           │
   └──────────────────────────────────────────────────────────────────────┘
```

Three levels of indirection, each doing one job:

- **Slot** (in tool): what the handler calls the thing. Stable across environments.
- **Bound name** (in system.yaml): what the project calls the thing. Stable across tools within the project.
- **Ref + version** (in system.yaml): what the foundry resolves to an on-disk factory. Stable across projects.

The indirection pays off the first time you run the same tool in dev and prod with different credentials — which is always.

## Module layout

```
src/foundry/auth/
├── __init__.py                AuthScheme enum re-exports
├── schemes/
│   ├── api_key.py             static key, header injection
│   ├── basic_auth.py          user/pass
│   ├── oauth2_client_creds.py client credentials flow
│   ├── oauth2_refresh.py      refresh-token flow
│   ├── jwt_bearer.py          JWT-assertion flow
│   ├── sigv4.py               AWS SigV4 signer
│   ├── mtls.py                mutual TLS (client cert/key)
│   └── custom.py              user-provided Callable
├── token_cache.py             short-lived token store w/ expiry
└── redactor.py                redact credentials in logs/traces

src/foundry/connections/
├── __init__.py                public helpers
├── pool.py                    ConnectionPool concrete impl
├── registry.py                ConnectionSpec discovery, factory loading
├── health.py                  health-check runner
├── descriptors.py             ConnectionDescriptor builder / redactor
└── errors.py                  local helpers raising ConnectionError subclasses
```

## Auth schemes

Each scheme in `foundry.auth.schemes` is a small helper the connection's `auth.py` composes with. A scheme does not know about the target system — Snowflake, Postgres, Slack are the factory's concern. The scheme knows how to *produce an authenticated HTTP header / signed request / TLS context / bearer token / etc.* given typed inputs.

### `api_key`

```python
class APIKeyConfig(BaseModel):
    header_name: str = "Authorization"
    value_template: str = "Bearer {api_key}"
    key_credential: str = "api_key"   # field name in ResolvedCredentials

async def apply(
    request_like: RequestLike,
    config: APIKeyConfig,
    credentials: ResolvedCredentials,
) -> RequestLike: ...
```

Covers ~60% of SaaS APIs. Key is read from `credentials`; header name/format are config. No refresh.

### `basic_auth`

Classic `Authorization: Basic base64(user:pass)`. Credentials carry `username` + `password`.

### `oauth2_client_credentials`

Server-to-server flow. Config carries `token_url`, `scopes`, `audience` (optional). Credentials carry `client_id` + `client_secret`. Scheme handles token fetch + caching + refresh-before-expiry.

```python
class OAuth2ClientCredentialsConfig(BaseModel):
    token_url: str
    scopes: list[str] = Field(default_factory=list)
    audience: str | None = None
    grant_type: str = "client_credentials"
    early_refresh_buffer_s: int = 60
```

### `oauth2_refresh_token`

User-delegated. Config carries `token_url` + `client_id`. Credentials carry `refresh_token` (long-lived) and optionally `access_token` (short-lived). Scheme rotates access token via refresh when expired.

### `jwt_bearer`

JWT-assertion flow (RFC 7523). Used by Google service accounts, Salesforce JWT bearer, GitHub Apps. Config carries `token_url`, `issuer`, `audience`, `subject`, `scopes`, `algorithm`, `expiry_s`. Credentials carry `private_key` (PEM) or `private_key_ref` (pointer to secret manager). Scheme signs the JWT and exchanges for an access token.

### `sigv4`

AWS Signature V4. Config carries `service` (e.g. `bedrock`, `s3`), `region`. Credentials carry `access_key_id`, `secret_access_key`, optional `session_token`. Scheme signs requests in-flight. Supports `kind=default` credentials for the default AWS SDK chain (env, IAM role, SSO profile, EC2 metadata).

### `mtls`

Client certificates for on-prem and internal services. Config carries `ca_bundle_ref` (trust). Credentials carry `client_cert` + `client_key` (PEM or DER, PKCS#12 supported). Scheme constructs an `ssl.SSLContext` and injects into the HTTP/DB client at build time.

### `custom`

Escape hatch. Connection's `auth.py` supplies an arbitrary async callable that performs auth. Credentials are passed through. Used for exotic flows (pre-shared session IDs, IP-allowlisted APIs, proprietary SSO). Documented as unportable; the meta-agent does not scaffold this scheme without explicit human instruction.

## Connection lifecycle

### Build

1. Project run starts → compiler resolves every `ConnectionBinding` in `SystemSpec.connections`:
   - Loads the referenced `ConnectionSpec` (from `catalog/connections/<name>/v<N>/connection.yaml` or `projects/<p>/connections/<name>/v<N>/`).
   - Validates `ConnectionBinding.config` against the `ConnectionSpec.config_schema`.
   - Resolves `ConnectionBinding.credentials_ref` via `SecretsProvider`.
   - Imports the factory from `ConnectionSpec.factory` (`auth.py::build_connection`).
   - Computes `config_hash` = hash(canonical-json(resolved_config - non_sensitive_config_fields)).
2. At first tool invocation that needs the slot, `ConnectionPool.acquire(ref, config_hash, project, factory, factory_args)` is called.
3. Pool cache miss → call `factory(config, credentials, ctx)` → returns `Connection`.
4. Pool caches under key `(ref, config_hash, project)`. Subsequent acquires return the cached `Connection`.

### Use

Tool handler:

```python
async def handle(inputs: QueryIn, ctx: RunContext) -> QueryOut:
    conn = await ctx.connections.get("warehouse")   # slot name
    # conn.client is a typed SnowflakeConnection (or equivalent)
    rows = await asyncio.to_thread(conn.client.execute, inputs.sql)
    return QueryOut(rows=rows)
```

The handler never sees credentials, never calls `auth.py`, never pools anything. It just asks for a slot and uses the client. Every `ctx.connections.get` emits a `foundry.connection` event with `event=acquire` or `event=cache_hit`.

### Refresh

Three triggers:

- **Scheduled** (`refresh.mode: periodic`): background task refreshes on interval.
- **Expiry-aware** (`refresh.mode: expiry`): pool checks token TTL before every `acquire`; refreshes if within `early_refresh_buffer_s` of expiry. Default for OAuth.
- **On-auth-error** (`refresh.mode: on_auth_error`): tool handler raises `ConnectionAuthError` (because the underlying client got a 401); pool evicts the entry, retries the tool's outer call once. If the retry also 401s, the error propagates.

`refresh.mode: none` opts out entirely (useful for static API-key connections where credentials never rotate inside a process lifetime).

### Release

- Long-lived connections (DB pools, HTTP clients): `release` is a no-op; the pool keeps them.
- Per-acquire clients (some OAuth exchanges): `release` calls `conn.close()`.

Tool handlers do **not** call `release` — the runtime does at the appropriate lifecycle point (typically end of run for long-lived; end of tool call for per-acquire).

### Evict / close

- `ConnectionPool.evict(ref, project)` — triggered by `refresh` or by admin action (`foundry connections evict`).
- `ConnectionPool.close_all()` — on process shutdown; awaits all `.close()` calls with a per-connection timeout.

## Health checks

Every `ConnectionSpec` ships with an optional `health.yaml` (an `EvalSpec` with `scope: connection`). Its cases issue trivial operations against the real system: "SELECT 1", "GET /health", "auth.test" (Slack), etc.

Three contexts where health runs:

1. **CLI**: `foundry connections health <project>` runs every connection's health check. Exits non-zero on any failure. Useful pre-deploy.
2. **Meta-agent**: after `build_connection` completes, the meta-agent runs the health check via the `check_connection_health` tool. If it fails, the meta-agent iterates on the factory body (up to a budget) or surfaces the failure.
3. **Phase 9 startup probes** (opt-in): API server can probe connections at startup and refuse to serve if any required connection is unhealthy. Controlled by a project-level observability flag.

Health checks do NOT run automatically on every tool call — that would be expensive. They run on demand.

## Slot binding: compile-time validation

Every slot declared in a `ToolSpec.connections_required` must be wired. The compiler validates:

1. **Binding exists**: every non-optional slot has a matching key in `ToolBinding.connection_bindings`.
2. **Slot name matches**: the key in `connection_bindings` equals the `ConnectionSlot.slot` value.
3. **Bound name resolves**: the value in `connection_bindings` is a key in `SystemSpec.connections`.
4. **Accept prefix matches**: the `ConnectionBinding.ref@version` (or just `ref`, ignoring version if the accept entry omits `@`) matches at least one prefix in `ConnectionSlot.accepts`.

Failures surface as `CompileError` with clear context. Example:

```
CompileError: Tool 'query_snowflake' slot 'warehouse' is not bound.
  file: projects/pipeline_recon/system.yaml
  pointer: /tools/query_snowflake/connection_bindings
  declared slots: warehouse
  bound slots: (none)
  hint: Add `connection_bindings: {warehouse: <connection_name>}` and
        ensure `<connection_name>` appears in system.yaml's `connections:` block.
```

Compile-time is the latest point these errors can happen — there is no runtime path where an unbound slot surprises a tool mid-run.

## Credentials resolution

Every `ConnectionBinding.credentials_ref` is resolved once at compile via `SecretsProvider.resolve(ref) → ResolvedCredentials`. The resolved object is passed to the factory at build time.

`ResolvedCredentials` is a typed opaque wrapper. Its `__str__` / `__repr__` is redacted. It has:

```python
class ResolvedCredentials(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)
    scheme: AuthScheme
    fields: dict[str, SecretValue]
    """Scheme-specific fields (e.g. api_key, client_id+client_secret,
    access_key_id+secret_access_key, private_key)."""
    principal: str | None = None
    """Human-readable identity the credentials represent, if known.
    Surfaces in ConnectionDescriptor."""

class SecretValue(BaseModel):
    """Wrapper around a secret string that never serializes its value."""
    _value: str = PrivateAttr()
    def reveal(self) -> str: return self._value
    def __str__(self) -> str: return "<redacted>"
    def __repr__(self) -> str: return "<SecretValue redacted>"
```

Factories call `.reveal()` to get the raw value when they need to sign a request or construct a client. No other code path reads the raw value.

## Observability

### Events

Every connection lifecycle event emits a `foundry.connection` OTel span + metric (see attribute spec in `01-architecture-overview.md` § Observability):

- `event=acquire` — pool cache miss; factory ran; new Connection built.
- `event=cache_hit` — pool cache hit; no factory call.
- `event=refresh` — token/cert refreshed.
- `event=release` — caller released the connection.
- `event=evict` — pool evicted (refresh or admin).
- `event=health_check` — health ran.

All events carry `ConnectionDescriptor` fields as attributes.

### Attribute redaction

`ConnectionDescriptor.redacted_config` contains only fields named in `ConnectionSpec.non_sensitive_config_fields`. The redactor is opt-in: fields not listed are dropped, not included-by-default. Sensitive-looking patterns (AWS key regex, secret-ish names) in config values are double-checked by the redactor as a safety net — a listed field whose value matches a secret pattern is still dropped, and a warning is logged.

### Cross-event correlation

Every `foundry.tool` event includes a `connections_used: list[ConnectionDescriptor]` attribute listing which connections the tool acquired during its call. Makes "which tool calls hit prod Snowflake today" a trivial query.

### Principal tracking

Connections carry a `principal` where the auth scheme can identify it — OAuth client id, AWS caller identity, Snowflake user, GitHub App id. Emitted on every event. Downstream monitoring can answer "who authed as what against what."

## Meta-agent integration

### `list_connections`

Returns:
- Every catalog connection with available versions, LATEST pointer, auth scheme, tags, short description.
- Every project-local connection (same).
- Every bound connection in the current project's `system.yaml` with its pinned version.

Used by the meta-agent to decide whether to bind an existing catalog connection or scaffold a new one.

### `build_connection`

Scaffolds a new project-local connection:

```
projects/<p>/connections/<name>/v1/
├── connection.yaml       ConnectionSpec with placeholder factory path
├── auth.py               stub with the declared auth_scheme's helper imports
├── schemas.py            Pydantic config schema skeleton
├── health.yaml           EvalSpec skeleton with one placeholder case
└── README.md
```

Meta-agent then fills in:
- Config-schema fields based on the target system's documented parameters.
- Factory body using the appropriate `foundry.auth.schemes.*` helper.
- Health check case that exercises a trivial read-only call.

Runs `check_connection_health` before committing. If the health check fails, iterates on `auth.py` + `schemas.py` (bounded attempts).

### `describe_connection`

Returns the `ConnectionDescriptor` for a bound connection — safe for the meta-agent to reason about. Does NOT include credentials.

### `check_connection_health`

Runs the connection's `health.yaml` via the eval harness; returns pass/fail + latency + per-case details.

### Guardrails

- Meta-agent MUST NOT write to `catalog/connections/*` (same sandbox rule as tools).
- Meta-agent MUST NOT populate raw secret strings in `ConnectionBinding.config` — the secret-literal scan will reject them.
- Meta-agent MUST NOT change `auth_scheme` of an existing connection version — auth scheme changes require a new version (`v2/`). Enforced by schema validator on write.
- Meta-agent's prompt explicitly describes these constraints.

## Security considerations

### Defense in depth

- **Loader**: secret-literal scan on `ConnectionBinding.config` rejects embedded credentials.
- **Runtime**: credentials resolved through `SecretsProvider`; never read from YAML.
- **Logging**: `SecretValue.__str__` is always redacted. Log formatters never attempt to deep-inspect `ResolvedCredentials`.
- **Tracing**: `ConnectionDescriptor` is the only data surfaced. Span processors assert no field named in a denylist (`api_key`, `password`, `secret`, `token`, `private_key`) appears in exported spans.

### Principle of least privilege

Bound credentials should scope to what the tool needs: read-only roles for query tools, write-scoped for write tools, separate connections for separate scopes. The foundry doesn't enforce this (can't know), but:
- `build_connection` meta-agent prompt encourages narrow scoping in config suggestions.
- `describe_connection` surfaces `principal` so humans reviewing configs can see what identity is being used.
- Audit stream makes over-privileged credentials visible retroactively.

### Secret rotation

Rotation workflows are per-secret-store (Vault lease renewal, AWS Secrets Manager rotation Lambdas, etc.). The foundry's role: respond to rotation correctly.

- `ConnectionBinding.credentials_ref` resolves via `SecretsProvider.resolve()`, which MAY return a fresh value each call. When rotation happens, the next `acquire` (or the `refresh` trigger) picks up the new value.
- For schemes with short-lived tokens derived from long-lived credentials (OAuth client creds, JWT bearer), the token cache expires on schedule and re-derives from the then-current credentials.
- Manual force: `foundry connections refresh <project>/<name>` evicts the pool entry; next acquire re-resolves credentials AND rebuilds.

## Versioning connections (how it fits the three-axis model)

Connections follow the **tool versioning axis** — directory-per-version, pinned in `system.yaml`, immutable once committed.

```
catalog/connections/snowflake/
├── v1/  (password auth — initial)
├── v2/  (key-pair auth)
├── v3/  (OAuth SSO)
├── versions.json
└── LATEST → v3
```

Projects pin explicitly (`version: v2`). Bumping across versions may require config shape changes (`v1` used `password` field; `v2` uses `private_key_ref`). The compile-time config-schema check catches incompatible upgrades:

```
CompileError: ConnectionBinding config incompatible with connection version.
  file: projects/pipeline_recon/system.yaml
  connection: catalog/snowflake@v2
  missing required fields: private_key_ref
  unexpected fields: password
  hint: catalog/snowflake@v2 uses key-pair auth. See catalog/connections/snowflake/v2/README.md.
```

Rollback to an earlier version: pin edit. Same as tool rollback. Same audit log entry.

## Pooling semantics (normative)

1. **Pool keys** are `(ref, config_hash, project)`. Two projects binding the same catalog connection with the same config get separate pool entries (principal may differ by project).
2. **Coalescing**: concurrent `acquire` calls for the same key with a cold cache wait on a single factory invocation. No thundering herd.
3. **Concurrency cap**: `PoolPolicy.max_concurrent` per pool entry. `acquire` waits up to `acquire_timeout_s` for a slot; exceeding raises `ConnectionPoolExhausted`.
4. **Idle eviction**: if `PoolPolicy.idle_ttl_s` is set, a background task evicts entries not acquired for that long. Default `None` (never).
5. **Graceful shutdown**: `close_all()` awaits every `.close()` with a per-connection timeout (default 10s); forced-close after timeout with an audit log entry.

## Failure modes

| Cause | Surfaced as |
|---|---|
| Slot not declared in ToolSpec but `ctx.connections.get(slot)` called | `ConnectionSlotNotDeclaredError` (runtime) |
| Slot declared but not bound in SystemSpec | `ConnectionSlotNotBoundError` (compile) |
| Bound connection ref doesn't match slot's accepts | `CompileError` |
| Bound connection config fails config_schema validation | `ConfigValidationError` at load |
| Credentials ref unresolvable | `ConnectionAuthError` at build |
| Factory raises during build | `ConnectionAuthError` if auth-looking, `ConnectionConfigError` otherwise |
| Health check fails | `ConnectionHealthCheckError` with per-case details |
| Token refresh fails | `ConnectionRefreshError` wrapping the upstream |
| Pool concurrency cap reached | `ConnectionPoolExhausted` |
| Build exceeds timeout | `ConnectionTimeoutError` |

## Invariants

1. **Tool handlers never touch credentials.** Read-through: grep `src/foundry/` for `.reveal()` calls — they appear only in `foundry/auth/` and `foundry/connections/`. Any hit in a tool handler is a bug.
2. **Connections are typed end-to-end.** No handler does `Any` — either via `TypeVar` binding or the handler's explicit cast against the documented `client_type`.
3. **Pool acquires are idempotent on the (ref, config_hash, project) key.** Two acquires → one factory call.
4. **ConnectionDescriptor never contains secrets.** Enforced by redactor allowlist + export-time denylist filter.
5. **Slot names are compile-validated.** A bug where a tool calls `ctx.connections.get("warehuose")` fails compile when that typo doesn't appear in `connections_required`, not at runtime.
6. **Factories never log credentials.** Inside a factory, `credentials.fields["..."].reveal()` is the only read path; the `.reveal()` function does not log.

## Test expectations

### Unit

1. **Scheme helpers**: each of the 8 schemes has per-scheme tests asserting header/signer output for known inputs.
2. **Config schema round-trip**: for each seeded catalog connection, construct a binding, validate, dump, re-load, assert equality.
3. **Descriptor redaction**: construct `ConnectionDescriptor` from a config containing a listed-allowlist field + a listed-denylist-regex field; assert the descriptor contains only the allowlist entry.
4. **Pool coalescing**: fake factory with artificial delay + concurrent acquires; assert only one factory call happened.
5. **Refresh triggers**: for each `refresh.mode`, simulate the trigger; assert eviction and rebuild.
6. **SecretValue non-leak**: `str(credentials)` and `repr(credentials)` never contain the raw value. Formatter test: `logging` format string including the credentials object never emits the value.

### Contract

1. **No credential leak in traces.** A test run with a known fake key; assert the key does not appear anywhere in exported OTel spans.
2. **`ConnectionDescriptor` JSON-serialisable.** Same pattern as `FoundryError.to_dict()`.
3. **Slot-binding static check.** A test fixture with a declared-but-unbound slot in a project compiles with a `CompileError`; fixture with a bound slot of the wrong accepts prefix also fails.

### Integration (Phase 2 exit gate)

1. **End-to-end with a catalog connection**: trivial project binds `catalog/http_service@v1` via `api_key`, calls a tool that uses the connection, returns the typed response.
2. **Version swap**: change pin `v1 → v2` where `v2` differs in `auth_scheme`; run again; assert new auth path exercised.
3. **Two tools, one connection**: both tools declare the `warehouse` slot bound to the same `prod_snowflake`; assert pool cache hit on the second tool's acquire (metric sanity check).
4. **Health check**: `foundry connections health <project>` runs and passes; corrupting the credential ref makes it fail with `ConnectionHealthCheckError`.

## Operational CLI surface (previewed; detailed in `82-dev-ux.md`)

- `foundry connections list [--project <p>]` — tabular view of bindings + catalog availability.
- `foundry connections health [<project>] [--slot <name>]` — run health checks; exit non-zero on failure.
- `foundry connections refresh <project>/<name>` — force-refresh a pool entry.
- `foundry connections describe <project>/<name>` — print `ConnectionDescriptor` for a bound connection.
- `foundry catalog promote <project>/connection/<name>` — Phase 5 tool, human-gated.

## Open questions

1. **Connection health as part of project-level eval?** Currently health lives in `health.yaml` per connection and runs on demand. Consider: the project-level end-to-end eval optionally runs connection health as a precondition and short-circuits with a clear message if any connection is down. Lean: yes, opt-in flag on the project-level EvalSpec.
2. **Cross-project connection pool sharing.** Currently pool key includes project; two projects binding the same catalog connection with the same config get separate pool entries. Is that right? Lean: yes for isolation, but allow an opt-in `shared_pool: true` on `ConnectionBinding` for known-safe cases.
3. **Connection templates in the catalog?** `catalog/agent_templates/` is proposed; parallel `catalog/connection_templates/` (partial factory + schema skeleton for a family of similar systems — e.g. "SQL database over JDBC") could speed `build_connection`. Defer to post-v1.
4. **Connection eval scoring in `compare_versions`.** `foundry eval compare --connection <name> v1 v2` runs each version's `health.yaml` against the real system and compares reliability/latency. Useful for rollouts. Recommend: yes, same UX as tool compare. Add to Phase 4 if time permits, else Phase 5.
5. **Does the meta-agent scaffold `custom` scheme at all?** Risk: it'll invent auth flows that don't exist. Recommend: no — require an explicit human instruction `--allow-custom-auth` on the forge call.
