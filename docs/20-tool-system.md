# 20 — Tool System

## Purpose

Tools are how an agent interacts with the world beyond its LLM. Every external query, every side-effecting action, every typed transformation is a tool. This doc specifies the full surface: how tools are authored, packaged, versioned, registered, allowlisted, dispatched, validated, sandboxed, cached, and observed.

The `Tool` protocol itself is defined in `10-core-framework.md`. The `ToolSpec` Pydantic schema is defined in `12-config-and-validation.md`. Connections (auth) are in `23-connections-and-auth.md`. Tool-result caching is in `24-caching-and-optimisation.md`. This doc is the consolidating spec — the one place a tool author looks to understand the whole shape.

Two load-bearing properties:

1. **Tools are versioned, shareable, and pinned.** A tool is a directory with a fixed file shape under `catalog/tools/<name>/v<N>/` (shared) or `projects/<name>/tools/<name>/v<N>/` (project-local). Projects pin specific versions. Rolling forward is deliberate.
2. **Tool handlers never touch credentials.** They request connections by slot via `ctx.connections.get(slot)` and receive typed authenticated clients. Auth, pooling, refresh, retries, audit — all framework concerns.

## Tools are handmade Python (the asymmetry with agents)

The foundry treats agents and tools asymmetrically on purpose:

| | Agents | Tools |
|---|---|---|
| Config | YAML + markdown prompt + Pydantic output schema | YAML + Pydantic schemas |
| Behaviour | LLM-driven; declarative | Imperative Python handler |
| Authoring | Easy to scaffold, easy to iterate via prompt edits | Real engineering; meta-agent helps but doesn't replace judgement |
| Versioning | Content hash over config + prompt | Frozen `v<N>/` directory; immutable once committed |
| Meta-agent role | Generates and iterates prompts; high quality on most tasks | Scaffolds structure, fills handler body for known patterns; needs human review |
| Failure mode if wrong | Prompt edit + re-eval; cheap | Code change + new version; more expensive |
| Catalog promotion | (agents are project-scoped; templates exist for archetypes) | First-class — catalog tools are a primary asset of the framework |

This asymmetry is deliberate. Tools touch real systems with real consequences (DB writes, emails sent, money moved); LLMs writing arbitrary tool code unsupervised is a recipe for production incidents. The framework structurally constrains what tools can do (the 5-file shape, the connection-slot pattern, the typed schemas, the standalone eval) so even meta-agent-written tools fall within safe bounds.

### What this means in practice

**For simple, well-understood tools** (HTTP GET against a typed REST API, SQL query against a documented schema, sending a Slack message, reading an S3 object) — meta-agent's `build_tool` produces a working first-pass. Standalone eval catches obvious bugs. Human review before catalog promotion is cheap.

**For complex, domain-specific tools** (proprietary internal API, multi-step protocol with retry semantics, anything calling a system the meta-agent has no documentation for) — meta-agent scaffolds the structure (5 files, schemas, tool.yaml). Human writes the handler body. Meta-agent can refine via standalone eval iteration.

**For dangerous tools** (`dangerous: true` — code execution, arbitrary URL fetch, side-effecting actions without strict input constraints) — meta-agent does not scaffold. Human writes; explicit `--allow-dangerous-tools` flag required for `forge` to even surface them as candidates.

### Why the catalog matters here

The catalog is the foundry's bet that **the asymmetry compounds in your favour over time**:

- Quarter 1: most tools are project-local, often human-written. Catalog is small. Meta-agent's tool-writing impact is modest.
- Quarter 3: catalog has 30+ shared tools. New projects pin from catalog ~80% of the time. Meta-agent's `build_tool` is a fallback for genuinely project-specific tools.
- Quarter 6: catalog quality is high (each tool battle-tested across multiple projects); meta-agent's job is "which catalog tool fits" not "write a new one."

The meta-agent's job is bounded by what's in the catalog. Investing in catalog quality early pays off in meta-agent reliability later.

## Preprocessing: where it goes

When a tool needs preprocessing (input transformation, validation, audit, authorization), there's a decision tree for where to put it. The wrong choice creates duplication or pollutes concerns.

### Decision tree

```
Is the preprocessing about the inputs to THIS tool only?
   │
   ├─ YES → Inside the tool handler, before connection acquisition.
   │        (Most common case. Pure handler code.)
   │
   └─ NO → Is it cross-cutting across MANY tools?
       │
       ├─ Auth, audit logging, rate limiting → In the connection.
       │  The connection's auth.py wraps the returned client to add
       │  logging, retries, audit; tools that use the connection inherit.
       │
       ├─ Multi-tool input transformation → A function node before the
       │  agent that calls the tools. Computes the normalised inputs once,
       │  writes to state; downstream tools read from state.
       │
       ├─ Per-call dynamic policy ("can this user/agent call X with Y?") →
       │  A "guard tool" the agent must call first. Explicit in the
       │  agent's allowlist; returns approved/rejected; downstream tool
       │  reads the verdict from state OR refuses if the verdict isn't there.
       │
       └─ Pure-Python pre-API-call validation → Middleware before
          the foundry API endpoint. Out of foundry scope; lives in the
          consuming pipeline.
```

### Examples

**Inside the tool handler** — input format normalisation:

```python
# handler.py
async def handle(inputs: QueryIn, ctx: RunContext) -> QueryOut:
    # Preprocessing: normalise SQL whitespace, validate parameter binding format
    sql = re.sub(r'\s+', ' ', inputs.sql.strip())
    if any(p.startswith('%') for p in inputs.parameters):
        raise ToolInputValidationError("Use pyformat parameter binding (%(name)s), not %s")
    
    conn = await ctx.connections.get("warehouse")
    ...
```

**In the connection** — audit logging that applies to every tool using it:

```python
# catalog/connections/snowflake/v3/auth.py
async def build_connection(config, credentials, ctx):
    raw_client = build_snowflake_client(config, credentials)
    return AuditingSnowflakeConnection(
        client=raw_client,
        audit_callback=lambda sql, params: emit_audit_event(...)
    )
```

Every tool using `catalog/snowflake@v3` gets audit logging; tool authors don't think about it.

**A function node** — multi-tool input transformation:

```yaml
# system.yaml
agents: [investigator]
functions: [enrich_trade_context]

flow:
  type: sequential
  steps: [enrich_trade_context, investigator]
```

The function fetches reference data (counterparty names, market mid prices) once, writes to state. The investigator agent's tools (`query_snowflake`, `validate_deltas`) read from the enriched state — no per-tool re-fetching.

**Guard tool** — per-call dynamic policy:

```yaml
# agent.yaml
tools: [check_authorization, send_email, send_slack, escalate_to_human]
```

The agent's prompt instructs it to call `check_authorization(action, args)` before any side-effecting tool. The check tool returns approved/rejected based on policy state; downstream tools refuse if the most recent authorization in state doesn't match.

This pattern is enforceable via lifecycle hooks too (after_tool checks the previous tool was an authorization).

### Why preprocessing is rarely a function node

Because most preprocessing is concern-specific (one tool's input shape, one connection's audit, one policy's check), function nodes are usually overkill. Function nodes shine when the preprocessing serves multiple downstream consumers — which is rare for tool inputs but common for run-wide context enrichment.

Don't reach for function nodes as the default preprocessing answer. Use them when state-shape transformation is the thing.

## Module layout

```
src/foundry/core/
└── tool.py                Tool protocol, BaseTool, ToolRegistry, RunContext, RetryPolicy

src/foundry/<consumer modules use ToolRegistry>
```

Concrete tool *handlers* live in `catalog/tools/<name>/v<N>/handler.py` or `projects/<name>/tools/<name>/v<N>/handler.py` — outside `src/foundry/`. They are user code, loaded by the `ToolRegistry` at compile time via importlib.

## The 5-file shape

Every tool version is a directory with these files. The shape is enforced by the `ToolRegistry` loader — missing files fail at compile time with a clear error.

```
<root>/tools/<name>/v<N>/
├── tool.yaml            ToolSpec (the contract)
├── handler.py           async def handle(inputs, ctx) -> output
├── schemas.py           Pydantic input + output models
├── eval.yaml            standalone tool-level EvalSpec (REQUIRED by the loader)
└── README.md            what it does, when to use, edge cases, gotchas
```

> **Erratum (Phase 2a, implemented behaviour):** the loader enforces the full
> 5-file shape — `eval.yaml` is a REQUIRED file, not optional. A version
> directory missing it fails at compile time. What stays a matter of
> judgement is the eval's *quality* (case count, scorer choice), not the
> file's presence.

### `tool.yaml`

The `ToolSpec` (full schema in `12-config-and-validation.md`). Recap:

```yaml
name: query_snowflake
version: v2
description: |
  Run a parameterised read-only SQL query against the bound Snowflake
  warehouse. Results returned as a list of rows; large result sets are
  truncated with a flag.

input_schema: schemas.py::QueryIn
output_schema: schemas.py::QueryOut

handler: handler.py::handle

timeout_s: 30.0
retry_policy:
  max_attempts: 3
  backoff: exponential
  initial_delay_s: 1.0
  retryable_errors:
    - ProviderTimeoutError
    - ConnectionTimeoutError

overridable_settings: [timeout_s, retry_policy]

connections_required:
  - slot: warehouse
    accepts: [catalog/snowflake]
    description: Snowflake account/warehouse/role to run the query against.

cacheable: true
cache_ttl_s: 300
cache_scope: project

tags: [database, snowflake, read-only]

standalone_eval: eval.yaml

author: foundry-team
created_at: 2026-04-25T10:00:00Z
schema_version: 1
```

### `schemas.py`

Pydantic input + output models. Validated at the boundary on every call. Field names, types, descriptions, and constraints are the tool's contract — changing them is a major version bump.

```python
# schemas.py
from pydantic import BaseModel, Field
from datetime import datetime

class QueryIn(BaseModel):
    sql: str = Field(min_length=1, description="Read-only SELECT statement.")
    parameters: dict[str, str | int | float | bool] = Field(default_factory=dict)
    max_rows: int = Field(default=1000, ge=1, le=100000)

class QueryRow(BaseModel):
    columns: dict[str, str | int | float | bool | None]

class QueryOut(BaseModel):
    rows: list[QueryRow]
    row_count: int
    truncated: bool
    elapsed_ms: int
    warehouse_used: str
    queried_at: datetime
```

The schemas SHOULD be small, descriptive, and self-documenting. The meta-agent (and human readers) rely on the field descriptions to understand what a tool does.

### `handler.py`

The async handler. Receives a validated input instance and a `RunContext`; returns a validated output instance.

```python
# handler.py
from foundry import RunContext
from .schemas import QueryIn, QueryOut, QueryRow

async def handle(inputs: QueryIn, ctx: RunContext) -> QueryOut:
    # Acquire the connection bound to this tool's 'warehouse' slot.
    conn = await ctx.connections.get("warehouse")
    client = conn.client  # snowflake.connector.SnowflakeConnection

    started = ctx.session.tracer.now_ms()
    try:
        cursor = client.cursor()
        cursor.execute(inputs.sql, inputs.parameters)
        rows = cursor.fetchmany(inputs.max_rows + 1)
    except Exception as exc:
        # Wrap any underlying-client exception as a ToolHandlerError.
        # The framework will catch ToolError subclasses; anything else
        # leaks out as ToolHandlerError automatically (see § Error semantics).
        raise

    truncated = len(rows) > inputs.max_rows
    if truncated:
        rows = rows[:inputs.max_rows]

    columns = [d[0] for d in cursor.description]
    return QueryOut(
        rows=[QueryRow(columns=dict(zip(columns, r))) for r in rows],
        row_count=len(rows),
        truncated=truncated,
        elapsed_ms=ctx.session.tracer.now_ms() - started,
        warehouse_used=conn.descriptor().redacted_config.get("warehouse", "unknown"),
        queried_at=datetime.now(timezone.utc),
    )
```

#### Handler authoring rules (normative)

1. **Signature is fixed.** `async def handle(inputs: <InputModel>, ctx: RunContext) -> <OutputModel>`. Names matter — the registry introspects by name.
2. **Inputs are pre-validated.** When the handler receives `inputs`, it's already a validated `<InputModel>` instance. Don't re-validate.
3. **Output is validated by the framework**. Return an instance of the declared `<OutputModel>`. The dispatcher validates on receipt; mismatches raise `ToolOutputValidationError`.
4. **Connections via `ctx.connections.get(slot)`.** Never construct clients yourself. Never call `auth.py`. Never read secrets from environment.
5. **Don't store `ctx`.** It's valid for the duration of the call. Storing it on `self` or in a module-level dict is a bug.
6. **Don't release connections.** The pool owns lifecycle.
7. **Errors raise typed `FoundryError` subclasses.** See § Error semantics.
8. **Cancellation is cooperative.** Long blocking calls inside `await asyncio.to_thread(...)` MUST check `ctx.session.cancel_token.cancelled()` periodically or use timeouts.
9. **Cost-aware**: tools that themselves call LLMs (e.g., a sub-agent tool) MUST go through `Provider.generate()` so `Session.cost_budget` enforces. Side-channel LLM calls bypass the budget and are bugs.

### `eval.yaml`

Standalone tool-level eval. Same `EvalSpec` schema as agent / project evals, but with `scope: tool` and `target: <ref>@<version>`. Cases are direct input → expected output.

```yaml
name: query_snowflake_v2_eval
description: Verify query_snowflake@v2 against a known test schema.
scope: tool
target: catalog/query_snowflake@v2

cases:
  - id: simple_select
    input:
      sql: "SELECT 1 AS one"
    expected:
      rows:
        - columns: { one: 1 }
      row_count: 1
      truncated: false
    weight: 1.0

  - id: parameterised
    input:
      sql: "SELECT %(name)s AS greeting"
      parameters: { name: "hello" }
    expected:
      rows:
        - columns: { greeting: "hello" }
      row_count: 1

scorers:
  - kind: exact
    name: row_match
    weight: 1.0

threshold: 0.95
deterministic: true
schema_version: 1
```

The standalone eval runs against a real (or test-double) connection. Results live alongside the tool version's metadata in `versions.json`. `foundry eval compare --tool query_snowflake v1 v2` runs both versions against the same eval cases and produces a delta report (`24-caching-and-optimisation.md` cross-references this).

### `README.md`

What the tool does, when to use it, gotchas, edge cases. Markdown. The meta-agent reads the README via `read_file` when it needs to decide whether to assign a tool to an agent. Quality of the README directly affects meta-agent quality of selection.

Format guidance:

```markdown
# query_snowflake

Run a parameterised read-only SQL query against a Snowflake warehouse.

## When to use

- Pulling reference data (employee directory, product catalogue, historical
  trade records).
- Joining data across tables for an investigation.

## When NOT to use

- Writing data — this tool is read-only by design. Use `write_to_snowflake`
  for inserts.
- Streaming large result sets — `max_rows` capped at 100k; use the
  `bulk_export` tool for larger queries.

## Connections required

- `warehouse` — must be a `catalog/snowflake@v1` or later. The role
  bound MUST have read-only privileges on the queried tables.

## Edge cases

- `max_rows` truncation: when result set exceeds `max_rows`, the
  `truncated` flag is set; agents should re-query with a tighter
  filter rather than incrementing `max_rows` blindly.
- Parameter binding: parameters are pyformat (`%(name)s`), not
  question-mark style.
```

## ToolRegistry

```python
class ToolRegistry:
    """Loaded once at compile time. Holds resolved Tool instances
    keyed by canonical ref + version. Provides lookup, listing,
    and the dispatch path for tool calls."""

    def get(self, ref: str, version: str) -> Tool: ...
    def list_all(self) -> list[ToolDescriptor]: ...
    def list_by_tag(self, tag: str) -> list[ToolDescriptor]: ...

    async def dispatch(
        self,
        ref: str,
        version: str,
        agent_allowlist: list[str],
        raw_input: dict[str, Any],
        ctx: RunContext,
    ) -> Any:
        """The single entry point through which tool calls flow.
        Validates input, checks allowlist, runs handler with retry +
        timeout + caching + observability."""
```

### Loading

At project compile time:

1. Read `SystemSpec.tools` map; for each `ToolBinding`, resolve `ref@version` to a directory via `FoundryRoots.catalog_roots` + `projects_root`.
2. Load `tool.yaml` → `ToolSpec`.
3. Load `schemas.py` and `handler.py` via `importlib`. Resolve `input_schema` / `output_schema` / `handler` references.
4. Validate that the handler signature matches `async def handle(inputs: <InputModel>, ctx: RunContext) -> <OutputModel>`.
5. Validate `connections_required` slots match `ToolBinding.connection_bindings` keys.
6. Construct a `Tool` instance and register under canonical key `(ref, version)`.

Failures at any step → `ConfigError` or `CompileError` with the failing tool's path.

### Dispatch

When an agent makes a tool call:

```
1.  Allowlist check
    └─ if tool name not in agent's tools allowlist → ToolNotAllowedError

2.  Resolve to (ref, version) from SystemSpec.tools binding
    └─ if not found → ToolNotFoundError

3.  Validate raw_input against input_schema
    └─ if invalid → ToolInputValidationError

4.  Compute input_hash for observability + caching

5.  Cache lookup if ToolSpec.cacheable
    └─ hit → emit cache.tool.hit event, return cached output

6.  Acquire connections in connections_required
    └─ each slot resolved via ctx.connections.get(slot)

7.  Open foundry.tool span; emit tool.started event
    └─ attributes: tool_ref, tool_version, input_hash, agent

8.  Run handler under anyio.fail_after(timeout_s)
    │  + retry loop per retry_policy
    │  + cancellation propagation via cancel_token

9.  Validate output against output_schema
    └─ if invalid → ToolOutputValidationError

10. Cache store if ToolSpec.cacheable
    └─ emit cache.tool.store event

11. Emit tool.completed event
    └─ attributes: success, latency_ms, retry_count, output_summary

12. Return validated output
```

Every step that can fail emits a structured event before raising. The audit trail is complete even when tools fail.

## Allowlisting

`AgentSpec.tools: list[str]` is the allowlist. Names are logical names from `SystemSpec.tools` keys, NOT refs. Example:

```yaml
# SystemSpec
tools:
  query_snowflake:
    ref: catalog/query_snowflake
    version: v2
  validate_deltas:
    ref: local/validate_deltas
    version: v3

# AgentSpec
tools: [query_snowflake]   # this agent can call query_snowflake but NOT validate_deltas
```

When the LLM emits a tool-use block for a tool not in the agent's allowlist:

- The dispatcher refuses with `ToolNotAllowedError`.
- The agent's loop receives the error as the tool-result content (no exception bubbles to the run).
- The LLM sees the structured error in its next turn and can recover (apologise, choose another tool, give up).

This is correctness-by-construction: even if a prompt hallucinates an unauthorised tool name, it cannot be called.

## Tool versioning

Recap from `01-architecture-overview.md` and `50-versioning-model.md` (when written):

- Each version is a frozen directory. Files within a `v<N>/` are immutable once committed.
- Bumping a tool means creating a new `v<N+1>/` directory; the project explicitly pins to upgrade.
- `versions.json` at the tool's parent directory tracks metadata: eval scores, deprecation status, author.
- Catalog promotion is human-gated and runs the standalone eval against a configurable floor.

### Backwards compatibility within a version

Once `tool.yaml` is committed, the input schema, output schema, and handler are frozen. Editing them in place is a bug — the directory is the source of truth and downstream consumers cache against it.

If a bug fix is needed, create `v<N+1>/` and bump pins in consuming projects. The bad version stays on disk for historical accuracy + rollback.

### Schema evolution across versions

| Change | Version bump | Notes |
|---|---|---|
| Add optional input field with default | minor (v1 → v1.1 if patch versioning, else v2) | Old callers continue to work. Catalog promotion warns. |
| Add output field | minor | Old consumers ignore. |
| Remove or rename input field | major | Catalog promotion warns aggressively (or blocks with `--strict-semver`). |
| Change input type | major | Same. |
| Change output type | major | Same. |
| Change semantic behaviour with same schemas | minor with explicit READMElsy note | Hardest to detect; eval should catch regressions. |

Currently the foundry uses simple integer versions (`v1`, `v2`); semver-style minor/patch is an open consideration in `50-versioning-model.md`. Either way: schema-breaking change → new major.

## Allowlisted operations / sandbox

Tool handlers run in the foundry process. No process isolation in v1. The sandbox is constructive — what tools are *allowed* to do — rather than enforced isolation.

### Tools MUST

- Use `ctx.connections.get(slot)` for external systems.
- Honour `ctx.session.cancel_token` for long-running work.
- Raise `FoundryError` subclasses on failure.
- Return `<OutputModel>` instances or raise.

### Tools MUST NOT

- Read environment variables for secrets directly. Use connections.
- `os.system`, `subprocess.run`, `eval`, `exec` on user-provided content. (Tools that intentionally execute code, e.g. a code-runner tool, are an explicit category that requires extra review and a `dangerous: true` flag in `ToolSpec`.)
- Make network calls to arbitrary URLs without going through a connection.
- Mutate state directly. Tools return outputs; agents apply outputs to state via the orchestration runtime.
- Store `RunContext` beyond the call.

### Tools that DO need to execute code or call arbitrary URLs

Some legitimate tools need this — a code-execution sandbox, a generic web fetcher, a webhook poster. They must:

- Set `dangerous: true` in `ToolSpec` (lints to surface in code review and meta-agent prompt).
- Document in the README exactly what's executed and why.
- Include input validation that constrains what's possible (allowed URL patterns, language whitelist, etc.).
- Have a standalone eval that exercises the dangerous behaviour against safe fixtures.

The meta-agent does not scaffold dangerous tools without an explicit human flag (`--allow-dangerous-tools` on `forge`).

## Error semantics

Tool handlers MAY raise:

- **`ToolError` subclasses**: `ToolInputValidationError`, `ToolOutputValidationError`, `ToolHandlerError`, `ToolNotAllowedError`, `ToolNotFoundError`. The dispatcher classifies and emits.
- **`ConnectionError` subclasses**: `ConnectionAuthError`, `ConnectionTimeoutError`, etc. when a connection-related issue occurs. The dispatcher passes these through (they bubble to the agent layer).
- **`ProviderError` subclasses**: when the tool itself calls an LLM (e.g., a sub-agent tool). Bubbles up.
- **`ApprovalRequired`**: tool needs human approval before proceeding. Caught by the orchestration runtime; pauses run.
- **`RunCancelled`**: cancellation propagated from session.
- **Other `Exception`**: caught by the dispatcher and wrapped as `ToolHandlerError(cause=<original>)`. The original exception type is preserved in `context["cause_type"]` for debugging.

The agent loop receives the error structured (not as a Python exception). The LLM sees:

```
{
  "tool_use_id": "...",
  "is_error": true,
  "content": [
    {"type": "text", "text": "ToolHandlerError: connection refused (...)"}
  ]
}
```

It can then choose to retry, escalate, or apologise. The framework does NOT auto-retry on `ToolHandlerError` (only on `retryable_errors` configured in the tool's `RetryPolicy`).

## build_tool scaffold (meta-agent path)

When the meta-agent decides a project needs a new tool not in the catalog, it calls `build_tool`. The scaffold creates:

```
projects/<scoped_project>/tools/<name>/v1/
├── tool.yaml          ← ToolSpec stub with name, version, description placeholders
├── handler.py         ← imports + empty async handle stub
├── schemas.py         ← empty Pydantic class stubs
├── eval.yaml          ← EvalSpec stub with one placeholder case
└── README.md          ← templated structure
```

The meta-agent then iteratively fills in:

1. **`schemas.py`** — input + output Pydantic models with field types and descriptions.
2. **`tool.yaml`** — full ToolSpec including `connections_required`, `tags`, `cacheable`, etc.
3. **`handler.py`** — handler body using configured connection slots.
4. **`eval.yaml`** — at least 3 cases that exercise the tool against the bound connection.

Then runs the standalone eval. If failures, iterates handler until the eval passes the configured threshold.

### Scaffold guards (meta-agent only)

- The meta-agent's prompt forbids setting `dangerous: true` (per `83-security-guardrails.md`).
- The meta-agent's prompt forbids inline credential strings — the scaffold's `connections_required` block is required for any tool that touches an external system.
- `cacheable: true` requires a paired `cache_ttl_s`; the meta-agent's prompt explains the safety (idempotent only).
- The meta-agent calls `check_connection_health` after creating a connection slot binding before iterating the handler — fail-fast on misconfigured auth.

## Tool tags and discovery

`ToolSpec.tags: list[str]` enables the meta-agent's `list_tools` to return filtered subsets:

```
list_tools(domain="recon")        → tools tagged 'recon'
list_tools(tags=["read-only"])    → tools tagged 'read-only'
list_tools(connection="snowflake") → tools whose connections_required.accepts include catalog/snowflake
```

Tags are not enforced — they're descriptive. Recommended conventions:

- Domain tags: `recon`, `compliance`, `support`, `research`.
- Permission tags: `read-only`, `read-write`, `dangerous`.
- System tags: `snowflake`, `slack`, `s3`, `salesforce`.
- Capability tags: `summarisation`, `extraction`, `classification`.

Tag conventions are documented in the catalog's `index.yaml` and in each tool's README.

## Standalone evals are behavioural contracts, not code-level tests

Worth being explicit about what a tool's `eval.yaml` is and isn't, because the line is fuzzy for deterministic tools.

A standalone tool eval is a **behavioural contract test**: given input X, the tool produces output Y (within scorer tolerance). It's:
- ✅ Stored as a typed `EvalRunResult` artifact for cross-version comparison.
- ✅ The catalog promotion gate (`foundry catalog promote` checks the eval score against a floor).
- ✅ Visible to the meta-agent — when scaffolding a project, the meta-agent reads `versions.json` and prefers tools with strong eval scores.
- ✅ The primary test surface for **non-deterministic tools** (LLM-using tools, tools whose output depends on a live system's state).

It is NOT:
- ❌ A replacement for code-level unit testing of `handler.py` (off-by-one bugs, error-path coverage, fixture-sensitive edge cases).
- ❌ A comprehensive integration test suite (use pytest with real or replayed connections for that).
- ❌ The right home for fuzz testing or property-based testing.

### Recommended testing posture per tool kind

| Tool kind | Eval cases | Pytest |
|---|---|---|
| **Deterministic, no external system** (pure math, validators, formatters) | 3–5 representative cases — enough for catalog gate + meta-agent visibility | Comprehensive unit tests for code coverage; this is most of the testing |
| **Deterministic, calls external system** (DB query, HTTP API) | 5–10 cases against a test fixture connection or replayed responses | Code-level unit tests + integration tests with mocked / replayed external system |
| **Non-deterministic (uses LLM internally)** | Comprehensive cases (10+); LLM-judge scorers with calibration | Pytest can cover code paths but cannot meaningfully test LLM output quality — eval is the primary surface |
| **Side-effecting (sends email, triggers RPA)** | Eval cases against test fixtures (real send blocked); assert structured output | Integration tests with mocked external endpoints |

Pytest fixtures for testing handler.py code are spec'd in Tier 8 (`82-dev-ux.md`) — `foundry.testing.RunContextFixture`, `MockConnection`, etc. The two surfaces complement; they don't overlap.

The mistake to avoid: building a comprehensive eval set for a deterministic tool because "every tool needs one." A 3-case eval is fine for catalog gate; pytest covers the rest. The mistake on the other side: skipping the eval for an LLM-using tool because "I have pytest." Pytest can't measure LLM output quality.

## Standalone tool eval workflow

Tools have their own eval set independent of any agent or project. The workflow:

1. **Author / meta-agent writes** `eval.yaml` cases. Cases are typed: input shape matches the input schema, expected matches the output schema or a scorer-specific shape.
2. **`foundry eval tool <ref>@<version>`** runs the cases against the bound connection (test or real). Stores result.
3. **`foundry eval compare --tool <name> v1 v2 v3 ...`** runs the same eval cases against multiple versions of the same tool; produces a comparison report (`24-caching-and-optimisation.md` § Eval comparison).
4. **Catalog promotion** (`foundry catalog promote`) refuses if the standalone eval score is below the configured floor (default 0.85).

Standalone evals are how tool quality is measured independently of agent quality. A tool that works in isolation but fails in an agent context is a prompt problem, not a tool problem; this separation makes diagnosis efficient.

## Lifecycle inside an agent step

For one tool call within a single agent step:

```
LLM emits tool_use block
    │
    ▼
ToolRegistry.dispatch(ref, version, allowlist, raw_input, ctx)
    │
    ├─ allowlist check ────────────────► ToolNotAllowedError on miss
    │
    ├─ input validation ───────────────► ToolInputValidationError on miss
    │
    ├─ cache lookup (if cacheable) ───► cache.tool.hit, skip handler
    │
    ├─ connection acquire ──────────────► ConnectionAuthError on fail
    │
    ├─ tool.started event
    │
    ├─ retry loop (per retry_policy):
    │     handler invocation under anyio.fail_after(timeout_s)
    │     ├─ on retryable error: backoff + retry
    │     ├─ on non-retryable error: bubble
    │     └─ on success: break
    │
    ├─ output validation ──────────────► ToolOutputValidationError on miss
    │
    ├─ cache store (if cacheable)
    │
    ├─ tool.completed event
    │
    ▼
output → LLM sees as tool_result block
```

Multi-tool-call rounds within the same agent step (parallel tool calls per `ContentBlock`) run via `anyio.create_task_group` — each tool call is a task in the group. Failure in one cancels siblings cleanly; aggregated tool results are returned to the LLM in a single user-message turn.

## Composition with caching and connections (recap)

- **Tool-result caching** (`24`): opt-in per tool via `cacheable: true + cache_ttl_s`. Caches by hash of validated input.
- **Connections** (`23`): tools declare slots, projects bind, runtime issues authenticated clients.
- **Cost budget** (Tier 1): tools that internally call LLMs go through `Provider.generate`, which checks `Session.cost_budget`.

These three are orthogonal and compose: a `cacheable: true` tool with a `connection_required` slot whose handler calls an LLM gets cache + auth + cost enforcement automatically.

## Failure modes

| Cause | Surfaced as | Caught where |
|---|---|---|
| Tool name not in agent allowlist | `ToolNotAllowedError` | dispatcher, before validation |
| Ref/version unresolvable | `ToolNotFoundError` | dispatcher |
| Input fails Pydantic validation | `ToolInputValidationError` | dispatcher |
| Output fails Pydantic validation | `ToolOutputValidationError` | dispatcher (post-handler) |
| Handler raises `ToolError` subclass | re-raised | dispatcher |
| Handler raises arbitrary `Exception` | wrapped as `ToolHandlerError(cause=...)` | dispatcher |
| Handler exceeds `timeout_s` | `ToolHandlerError(cause=TimeoutError)` | `anyio.fail_after` |
| All retries exhausted | last error re-raised | retry loop |
| Connection slot not bound at compile | `ConnectionSlotNotBoundError` | compile time |
| Run cancelled mid-handler | `RunCancelled` | propagates |

Every failure emits a `tool.completed` event with `success=false` and `error_category` set; the audit trail records what went wrong.

## Invariants

1. **Tools are stateless across calls.** No module-level state carries across handler invocations beyond what connections / caches explicitly model.
2. **Tool handlers never construct `RunContext`.** The framework constructs and passes; handlers consume.
3. **Tool handlers never close connections.** Pool owns lifecycle.
4. **Input and output validation always happens.** Even if the LLM provides perfect inputs, validation runs — it's the contract enforcement point.
5. **Allowlist is enforced at dispatch.** A handler may exist in the registry but be inaccessible to a given agent; the dispatcher refuses without consulting the handler.
6. **`cacheable: true` requires `cache_ttl_s`.** Validator at config load.
7. **`dangerous: true` tools are flagged in observability.** Every dispatch emits `dangerous: true` as a span attribute for ex-post review.
8. **Error structure is preserved.** Original-error type, message, and stack trace land in the audit record's `context` dict, even after wrapping as `ToolHandlerError`.

## Test expectations

### Unit

1. **5-file shape enforcement**: missing any of the 5 files fails compile with a clear error naming the missing file.
2. **Handler signature check**: a tool whose handler signature doesn't match `async def handle(inputs, ctx)` fails compile.
3. **Schema round-trip**: input + output schemas dump and load cleanly; constraints (min_length, ge, le) honoured.
4. **Allowlist enforcement**: agent without tool in `tools` allowlist + tool registered → dispatch returns `ToolNotAllowedError`.
5. **Input validation failure**: invalid input shape → `ToolInputValidationError` before handler is called.
6. **Output validation failure**: handler returns wrong shape → `ToolOutputValidationError`.
7. **Cache hit**: tool with `cacheable: true` called twice with same input → second call hits cache; `cache.tool.hit` event emitted.
8. **Cache config validator**: `cacheable: true` without `cache_ttl_s` → `ConfigValidationError` at load.
9. **Retry loop**: handler raises `ConnectionTimeoutError` 2× then succeeds → retry policy retries, dispatcher returns final result; `retry_count: 2` on the `tool.completed` event.
10. **Timeout**: handler sleeps past `timeout_s` → `ToolHandlerError(cause=TimeoutError)`; `tool.completed` event emitted with success=false.

### Contract

1. **No credential leak in tool spans**: a tool whose connection auth carries an API key — the API key never appears in any emitted observability data; `ConnectionDescriptor.redacted_config` is the only connection metadata in spans.
2. **Forbidden imports in handler scaffolds**: lint enforces no `os.environ`, `subprocess`, `eval`, `exec` in `handler.py` files unless `dangerous: true`. *(Deferred: this lint ships with the Phase 6 meta-tools — the meta-agent's `build_tool` scaffold path is what runs it. Phases 2a/2b load handlers without it.)*

### Integration (Phase 2 exit gate)

Already covered by the Phase 2 exit gates in `03-development-phases.md`. Additions specific to tool system:

- A tool that declares both a connection slot AND `cacheable: true` runs end-to-end with cache hits + connection pool reuse + correct event emission.
- A `dangerous: true` tool flagged in test fixtures emits the `dangerous` span attribute and is refused if the meta-agent tries to scaffold it without `--allow-dangerous-tools`.

## Open questions

1. **Tool semver format.** Currently `v<N>` integer-only. Worth supporting `v<major>.<minor>.<patch>`? Lean: stay integer-only for v1; revisit if real use cases need finer-grained pinning.
2. **Tool composition (tool calling tool).** A "high-level" tool that internally calls 2–3 lower-level tools. Possible today via the handler authoring pattern, but the dispatcher's allowlisting only applies to the agent's own tool list. Should the parent tool inherit allowlists or have its own? Lean: parent declares its own `internal_tools` list at compile; framework enforces.
3. **Dangerous tool review**. `dangerous: true` is currently a self-declared flag. Should there be a separate review step (e.g. catalog promotion of dangerous tools requires explicit reviewer signoff)? Lean: yes for catalog (Phase 5); local project-only dangerous tools have lighter checks.
4. **Standalone eval against test connections vs real.** Current design assumes eval runs against the bound connection (real). For pre-promotion validation, should the foundry support running standalone evals against a test fixture connection? Probably yes — `eval.yaml` can specify `test_connection_overrides:` to swap in fixtures. Defer to Phase 4 evals work.
5. **Tool input redaction**. `tool.started` event carries `input_hash` and `input_preview`; preview is truncated. Should fields named `password`/`token`/`secret` be auto-redacted from preview even if accidentally present? Lean: yes, ship the same denylist used in `ConnectionDescriptor.redactor`.
