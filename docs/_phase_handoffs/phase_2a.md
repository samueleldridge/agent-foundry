# Phase 2a handoff — tools + connections + catalog + state visibility

**Session date:** 2026-07-07/08
**Branch:** `main`
**Status:** Phase 2a implementation complete; awaiting AI review + operator
manual smoke test. No live API keys exist in the dev sandbox — every
live-path assertion below was verified against `httpx.MockTransport` fakes
with only the HTTP layer substituted (established Phase 1 pattern; one
shared transport now serves both the LLM adapters and connection factories
via `ConnectionContext.http_transport`).

## Pre-work landed first

1. `fix(providers)`: OpenAI `completion_tokens` includes reasoning tokens;
   the adapter now subtracts so `TokenUsage.output_tokens` and
   `reasoning_tokens` are disjoint and `estimate_cost` (output + reasoning
   at the output rate) bills completion tokens exactly once. Regression
   test pins the billed amount.
2. `docs(errata)`: docs/11 amended to the implemented httpx design
   (`_build_request` / `_parse_response` / `_classify_http_error`; no
   `_langchain_bridge.py`; SSE assembly deferred to Phase 3); docs/12's
   "config imports core only" corrected (config legally imports
   `ModelBinding` from providers per docs/01).

## What this session built

1. **`foundry.core.tool`** — full `ToolRegistry.dispatch` (allowlist →
   resolve → input validation → handler under retry + `asyncio.timeout` →
   output validation), `RegisteredTool` / `ToolDescriptor`,
   `validate_handler_signature`, tool.started/completed emission with
   secret-redacting input previews; `RunContext` gained `connections`.
2. **`foundry.core.connection`** — `SecretValue` +
   `ResolvedConnectionCredentials` (multi-field, redact-on-print,
   `.reveal()` the only read path); accessor protocol gained
   `on_auth_error()` / `release_all()`; `ConnectionContext.http_transport`
   for MockTransport-driven factories.
3. **`foundry.core.state`** — `apply_reducer` (APPEND / MERGE incl. set
   union / LWW / REPLACE_IF_SET).
4. **`foundry.config.refs`** — `ArtifactRef` (tool + connection kinds; one
   shared resolution path), `FoundryRoots` (multi-root catalogs via
   `FOUNDRY_CATALOG_ROOTS`, upward walk to the repo `catalog/` by default),
   missing version → `RefResolutionError` listing available versions,
   `ref_matches_accept`.
5. **`foundry.catalog`** — `CatalogIndex` (now lists `connections` too),
   `VersionsMetadata`, index + versions.json loaders with structured
   errors, `catalog_entries` listing, `load_tool_version` /
   `load_connection_version` with 5-file-shape enforcement and
   spec-vs-directory consistency checks.
6. **`foundry.auth`** — 8 scheme helpers (api_key, basic_auth,
   oauth2_client_credentials, oauth2_refresh_token, jwt_bearer, sigv4,
   mtls, custom), `TokenCache` (early refresh + per-key coalescing),
   redactor (allowlist projection + secret-pattern double-check).
7. **`foundry.connections`** — `InProcessConnectionPool` (keys
   `(ref, config_hash, project)`, cold-build coalescing, max_concurrent +
   acquire timeout → `ConnectionPoolExhausted`, `PoolMetrics`),
   `SlotConnectionAccessor` (ConnectionEvent emission, on_auth_error
   eviction, release_all), `prepare_connections` (binding config validated
   against the version's schema with missing/unexpected detail; credentials
   resolved once at compile), `validate_tool_connection_wiring`
   (`ConnectionSlotNotBoundError` naming the slot; accepts-mismatch
   `CompileError`), health runner over health.yaml.
8. **`foundry.orchestration.state_scope`** — StateSpec type parser,
   ProjectState Pydantic model with `Annotated[..., Reducer]` metadata,
   per-agent TypedDict views, structural projection
   (`AgentStateView.project_input` — forbidden fields literally absent),
   compile-time `StateVisibilityError` for holes/unknown fields/orphans.
9. **Providers** — native tool use: Anthropic tools array + tool_use /
   tool_result block serialisation; OpenAI function-calling + role:tool
   messages; responses parse tool calls into `ToolUseBlock`.
10. **Runtime adapter** — `compile_project` wires tools + connections +
    state with all compile-time checks; `run_project` runs the LLM ⇄ tool
    loop (parallel dispatch per round, `IterationLimitError`) and returns
    pool metrics.
11. **CLI + artifacts** — `foundry connections health <project>[/<name>]`
    (exit 0/1/2); run artifact gains `tool_calls.jsonl`; metadata gains
    `pins` + `connection_pool` metrics.
12. **Catalog seeds** — tools `utc_now@v1`, `word_count@v1`,
    `http_get_json@v1+v2` (v2 adds `url` to output — pin-swap demo);
    connections `http_service@v1` (api_key) `+v2` (basic_auth — auth-swap
    demo), `postgres@v1`, `pgvector@v1`, `cohere_rerank@v1`;
    `catalog/index.yaml`; per-artifact `versions.json` + `LATEST`.
13. **`projects/hello`** — `get_time` = `catalog/http_get_json@v1`, slot
    `service` → `time_service` = `catalog/http_service@v1`; prompt v2
    instructs the tool call; new integration suite
    `tests/integration/test_run_hello_tools.py`.

## Env vars for live runs

| Variable | Used by |
|---|---|
| `ANTHROPIC_API_KEY` | hello's model binding |
| `HELLO_SERVICE_API_KEY` | hello's `time_service` connection (api_key). worldtimeapi ignores auth — export any placeholder, e.g. `dummy`. For the connection-pin-swap test (v2 = basic auth) export a JSON object: `{"username":"u","password":"p"}` |
| `HELLO_TIME_BASE_URL` | optional; defaults to `https://worldtimeapi.org` |
| `FOUNDRY_CATALOG_ROOTS` | optional; default walks up from the project dir to the repo `catalog/` |
| `FOUNDRY_HOME` | optional; overrides `~/.foundry` artifact root |

## Hero command

```bash
export HELLO_SERVICE_API_KEY=dummy
uv run python -m foundry run projects/hello --input '{"name": "world"}'
uv run python -m foundry connections health projects/hello/time_service
```

## Deviations from the docs (all deliberate)

1. **Handlers import sibling schemas via `from schemas import ...`**, not
   docs/20's `from .schemas import ...` sketch. The 5-file version dir is
   flat (no `__init__.py`), so relative imports cannot work; the catalog
   loader aliases the already-imported sibling module as `schemas` while
   handler.py executes. Module identity is keyed by file path so
   isinstance-based output validation holds.
2. **`ToolRegistry.dispatch` signature** is
   `dispatch(name, agent_allowlist, raw_input, ctx, emit=None)` — logical
   name in, resolution internal — rather than docs/20's
   `(ref, version, allowlist, ...)`; the allowlist check is over logical
   names (SystemSpec.tools keys) exactly as docs/20 § Allowlisting says, so
   name-first is the consistent shape. `emit` is the runtime's
   sequence-stamping event callback.
3. **`ToolNotAllowedError` / tool failures do NOT abort the run** — per
   docs/20 § Error semantics they surface to the LLM as `is_error`
   tool_results (verified in integration tests). The manual checklist's
   old Test 4 expected a non-zero exit; rewritten to match docs/20.
4. **`EvalSpec.scope` gained `"connection"`** (docs/12 listed only
   tool/agent/project; docs/23 requires health.yaml to be a
   `scope: connection` EvalSpec).
5. **Health runner semantics (Phase 4 gap-fill):** each health.yaml case
   invokes the connection's `health()` probe (the factory's trivial
   operation — GET health_path, SELECT 1, ...); case `input`/`expected`
   payloads and scorers are not yet interpreted (eval harness is Phase 4).
6. **jwt_bearer supports HS256 only**; RS256/ES256 raise a structured
   error naming the missing `cryptography` dependency (not pinned in
   Phase 0). sigv4 `kind=default` AWS chain resolution deferred with it.
7. **Multi-field credentials convention:** a `credentials_ref` secret may
   be either the scheme's primary field (api_key → `api_key`, jwt_bearer →
   `private_key`, oauth2_refresh → `refresh_token`, custom → `secret`) or
   a JSON object naming fields (required for basic_auth /
   oauth2_client_credentials / sigv4 / mtls); an optional `"principal"`
   key is lifted into descriptor metadata. docs/23 left this unspecified.
8. **agent.yaml `state_visibility` vs state.yaml `visibility`** both exist
   in the schemas; compile now requires them to MATCH (mismatch →
   `StateVisibilityError`). Docs never say which wins; review may prefer
   dropping one of the declarations later.
9. **Parallel tool calls use `asyncio.gather`**, not the
   `anyio.create_task_group` + `failure_mode` semantics of docs/21 —
   per-tool failures already become is_error results, so sibling
   cancellation semantics only matter for non-tool errors; revisit in
   Phase 3 with the real orchestrator.
10. **Pool checkout accounting**: `max_concurrent` is enforced via a
    semaphore acquired per `acquire()` and released by the accessor's
    `release_all()` at end of tool call (docs/23 leaves release timing to
    the runtime). Idle-TTL eviction has no background task yet (2a has no
    long-running process); `idle_ttl_s` is stored but not acted on.
11. **`postgres`/`pgvector` factories lazily import `asyncpg`** (not a
    pinned dependency) and raise a structured `ConnectionConfigError` if
    absent. Their shape is load-tested; live DB behaviour is untested
    until 2b uses them.

## Interface notes for Phase 2b

- `ToolSpec` still has NO cache fields; add `cacheable`/`cache_ttl_s`/
  `cache_scope` + the paired validator in 2b, and slot the cache lookup
  between dispatch steps 4 and 6 in `ToolRegistry.dispatch` (the seam is
  the `_run_with_retries` call).
- `ArtifactRef.kind` is a Literal["tool", "connection"]; extend to
  `retriever` / `agent_template` and add `_KIND_SUBDIR` entries.
- `CatalogIndex` already has `agent_templates`; add `retrievers`.
- `pgvector` config carries `embedding_dimensions` — the 2b
  dimension-match compile check should read it from the prepared
  connection's validated config.
- `SlotConnectionAccessor` is constructed per tool call by
  `_dispatch_one` in the runtime adapter; retrievers/caches will want the
  same accessor against their own slot maps.
- The runtime adapter is still deliberately minimal; the real compiler
  lands in Phase 3 — don't grow `compile_project` further.

## Exit-gate confirmation

| Gate | How verified | Outcome |
|---|---|---|
| One-tool agent end-to-end: model → tool (pooled, authenticated connection) → tool result → model → final output | `test_one_tool_agent_end_to_end` (auth header asserted at the fake service; tool_result fed to turn 2; artifacts checked) | ✅ (mock) / ⏳ operator |
| Catalog tool ref AND connection ref resolve through the same code path | `ArtifactRef.resolve_path` is the single resolver; `test_resolve_tool_and_connection_share_one_code_path` | ✅ |
| Pin v1→v2 (tool OR connection) → next run uses v2, no other change | `test_tool_pin_v1_to_v2_changes_loaded_version` + `test_connection_pin_v1_to_v2_switches_auth_scheme` | ✅ |
| Tool output validated at the boundary; invalid → structured error | `test_wrong_output_shape_...` (unit) + `test_invalid_tool_output_...` (integration) | ✅ |
| Tool not in allowlist → registry refuses, error surfaces clearly | `test_allowlist_refusal_...` (unit) + `test_tool_not_in_allowlist_refused_and_surfaced_to_llm` (is_error tool_result per docs/20) | ✅ |
| Unbound slot → compile-time ConnectionSlotNotBoundError naming the slot | `test_unbound_slot_fails_compile_naming_slot` (also asserts NO run artifact was created) | ✅ |
| `accepts` mismatch → compile-time error | `test_accepts_mismatch_fails_compile` (+ unit variants incl. exact-version accepts) | ✅ |
| `foundry connections health <project>/<name>` runs health.yaml | `test_connections_health_runs_health_yaml` + failure path; CLI exit codes 0/1/2 | ✅ (mock) / ⏳ operator |
| Pool reuse across tool calls in same run (metrics) | `test_pool_reuse_across_two_tool_calls_in_one_run` (builds=1, cache_hits=1 in metadata) | ✅ |
| refresh.mode on_auth_error: 401 evicts + rebuilds | `test_401_evicts_and_rebuilds_connection_then_succeeds` (builds=2, evictions=1) + unit single-retry semantics | ✅ |
| Secret literal in connections.\*.config → ConfigLoadError | `test_secret_literal_in_connection_config_rejected_at_load` (value never echoed) | ✅ |
| State visibility: reading forbidden field → compile-time StateVisibilityError | `test_reading_undeclared_state_field_fails_compile` + `test_visibility_hole_...`; structural projection unit-tested (field literally absent) | ✅ |
| Reducers: append/merge/lww/replace_if_set semantics | `test_core_state.py` apply_reducer suite + reducer metadata on the compiled model | ✅ |
| Catalog index lists tools+connections with versions; missing version → structured compile-time error | `test_catalog_entries_list_...` + `test_missing_pinned_version_fails_compile_listing_available` | ✅ |
| Version directory layout + versions.json match spec | seeds follow the 5-file shape; `test_versions_metadata_loads_for_every_seeded_artifact` cross-checks dirs vs metadata | ✅ |
| Updated hello runs end-to-end against catalog tool + connection | hero-path integration test + `foundry run` smoke over MockTransport | ✅ (mock) / ⏳ operator |

## Definition-of-done confirmation

- `uv run ruff check src/ tests/` — zero violations.
- `uv run mypy --strict src/foundry/` — passes (137 files).
- `uv run pytest tests/` — 288 passed (Phase 1's 157 all intact after the
  hello fixture update).
- `run_id` threaded through tool events, connection events, tool_calls.jsonl.
- No secrets in code/configs/fixtures; integration tests assert the fake
  service key appears in neither artifacts nor descriptors; `SecretValue` /
  credentials reprs are redaction-tested.
- Scope check: no `cacheable`/`cache_ttl_s`/`cache_scope` on ToolSpec, no
  `semantic_cache`/`retrievers`/`memory` on AgentSpec, no
  `foundry.cache`/`foundry.retrieval`/`foundry.memory` implementations
  (core protocol stubs from Phase 1 remain stubs).
