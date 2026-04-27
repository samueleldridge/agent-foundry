# 61 — Meta-Tools

## Purpose

The meta-agent (per `60-meta-agent.md`) is a `foundry.Agent` whose tool surface is the set of **meta-tools** — the operations it can perform on the foundry's own artifacts (read configs, write configs, run evals, scaffold tools/agents/connections, commit + roll back). This doc enumerates the meta-tools, their signatures, contracts, sandbox rules, and error semantics.

Each meta-tool is a foundry `Tool` (per `20-tool-system.md`) — same protocol, same registry, same dispatch path. They live in `src/foundry/configurator/tools/` and are bound only to the meta-agent (their `tools` allowlist). Project agents cannot call meta-tools — they're framework-internal.

Three load-bearing properties:

1. **Meta-tools are the meta-agent's only operational surface.** Anything the meta-agent does to the foundry happens via these tools. There's no side-channel; the audit trail is complete.
2. **Each meta-tool is sandboxed.** Path constraints, branch constraints, forbidden-action checks happen at the tool level — before the underlying operation executes. Defence in depth.
3. **Tools fail loudly with structured errors.** The meta-agent reads tool errors and adapts. Crucial for the loop's robustness.

## Tool catalogue

Organised by domain. Each entry: name, signature, contract, sandbox, errors.

### Filesystem

#### `read_file(path: str) -> FileContent`

Read the contents of a file. Used by the meta-agent to:
- Inspect existing project artifacts (configs, prompts, schemas).
- Read catalog tools / connections / retrievers to understand what's available.
- Read README files for context on tool / connection usage.
- Read prior commits' files indirectly via `git_show` (which uses `read_file` under the hood).

```python
class FileContent(BaseModel):
    path: str               # canonical absolute path
    content: str            # full file contents (text)
    size_bytes: int
    modified_at: datetime
```

**Sandbox**: `path` MUST resolve (after canonicalisation, symlink resolution) to one of:
- `projects_root/<scoped_project>/**`
- `framework_root/**` (read-only)
- Any `catalog_roots/**` entry (read-only)

Files outside these roots: `ConfigError("read_file: path outside sandbox")`.

**Errors**:
- `FileNotFoundError` (typed) — file doesn't exist.
- `ConfigError` — sandbox violation.
- `IOError` — read failure (rare).

Binary files: rejected (`size_bytes > 1MB OR contains_null_bytes` → error). Meta-agent doesn't need to read binaries.

#### `write_file(path: str, content: str) -> WriteResult`

Write content to a file. Used by the meta-agent for prompt iterations, scaffold edits, pin updates.

```python
class WriteResult(BaseModel):
    path: str
    bytes_written: int
    is_new: bool            # true if file didn't exist before
    is_overwrite: bool      # true if file existed and content differs
```

**Sandbox**: `path` MUST resolve to `projects_root/<scoped_project>/**` only. Catalog and framework writes refused.

**Behaviour**:
- Creates parent directories if needed.
- Atomic via temp-file + rename (no half-written files visible).
- If `is_overwrite`, the prior content is committed via git first (per `git_commit` flow) so the change is auditable.
- Refuses to write binary content (text-only files only).
- Refuses to write files in immutable paths (`tools/<name>/v<N>/` after that version exists — see § path immutability).

**Errors**:
- `ConfigError("write_file: path outside sandbox")`.
- `ConfigError("write_file: path is in an immutable version directory")` — attempting to modify a frozen `v<N>/` directory.
- `IOError` — write failure.

#### Path immutability rules

Once a `v<N>/` directory exists with content (after a `build_tool` / `build_connection` produces it), the meta-agent cannot modify files inside that directory. Schema evolution requires a new `v<N+1>/` directory. Enforced at `write_file`.

The exception: `versions.json` at the parent level (e.g., `tools/<name>/versions.json`) IS writable — it tracks metadata that legitimately evolves (eval scores, deprecation status).

### Discovery

#### `list_catalog() -> CatalogIndex`

Return the full catalog index: tools, connections, retrievers, agent templates, with their available versions and metadata.

```python
class CatalogIndex(BaseModel):
    tools: list[CatalogEntry]
    connections: list[CatalogEntry]
    retrievers: list[CatalogEntry]
    agent_templates: list[CatalogEntry]

class CatalogEntry(BaseModel):
    ref: str                    # 'catalog/query_snowflake'
    versions: list[str]         # ['v1', 'v2', 'v3']
    latest: str                 # 'v3'
    description: str            # from the latest version's README first paragraph
    tags: list[str]             # union of tags across versions
    eval_scores: dict[str, float]  # version → score
```

The meta-agent calls this once per forge run (cheap; cached across the run). Used in design (which catalog entries exist?) and in scaffolding decisions (which version of a connection to bind?).

**Errors**:
- `RefResolutionError` — catalog index file missing or malformed.

#### `list_tools(filter: ToolFilter | None) -> list[ToolDescriptor]`

Return tools currently in scope: catalog tools + the project's local tools, with their pinned versions if pinned, latest available otherwise.

```python
class ToolDescriptor(BaseModel):
    ref: str
    pinned_version: str | None     # if pinned in current SystemSpec.tools
    available_versions: list[str]
    description: str
    tags: list[str]
    connections_required: list[str]   # slot names the tool needs
    cacheable: bool

class ToolFilter(BaseModel):
    tags: list[str] | None
    domain: str | None             # alias for a specific tag prefix
    accepts_connection: str | None # filter to tools that accept a connection ref
```

**Errors**: same as `list_catalog`.

#### `list_connections(filter: ConnectionFilter | None) -> list[ConnectionDescriptor]`

Return bound connections in the scoped project + all catalog connections, with auth scheme + pinned status.

```python
class ConnectionDescriptor(BaseModel):
    ref: str
    pinned_version: str | None
    available_versions: list[str]
    auth_scheme: AuthScheme
    description: str
    bound_in_project: bool         # true if pinned in SystemSpec.connections
    binding_logical_name: str | None  # the name in SystemSpec.connections if bound
```

The descriptor here is the **non-authenticating** descriptor (per `23-connections-and-auth.md`) — safe for meta-agent reasoning.

#### `list_agents() -> list[AgentDescriptor]`

Return agents in the scoped project: their names, model bindings, output schemas (high-level), tool allowlists, current prompt versions.

```python
class AgentDescriptor(BaseModel):
    name: str
    description: str
    model_binding: ModelBinding
    prompt_version: str
    output_schema_class: str       # e.g. 'output_schema.py::Investigation'
    tools: list[str]
    state_visibility: StateVisibility
    has_memory: bool
    has_semantic_cache: bool
    has_retrievers: bool
```

#### `list_function_nodes() -> list[FunctionNodeDescriptor]`

Same shape for function nodes (per `21-agent-system.md` § Function nodes).

#### `describe_connection(name: str) -> ConnectionDescriptor`

Return the full descriptor for a specific bound connection (the project-level binding, with its config + descriptor). Safe for meta-agent reasoning (no credentials).

#### `check_connection_health(name: str) -> ConnectionHealth`

Run the connection's health check (per `23-connections-and-auth.md` § Health checks). Returns ok/not-ok + latency + diagnostic message.

Used after `build_connection` + before pinning new connections in production.

### Scaffolding

#### `build_tool(name: str, description: str, kind_hint: str | None) -> BuildToolResult`

Scaffold a new project-local tool. Creates the 5-file shape (per `20-tool-system.md` § The 5-file shape) under `projects/<scoped_project>/tools/<name>/v1/`:

```
tools/<name>/v1/
├── tool.yaml          stub with name, version, description placeholders
├── handler.py         empty async handle stub
├── schemas.py         empty Pydantic class stubs
├── eval.yaml          stub with one placeholder case
└── README.md          templated structure
```

**`kind_hint`**: optional category guiding the scaffold templates. Values: `database_query`, `http_api_call`, `messaging`, `file_io`, `validation`, `transformation`, `custom`. Affects starter templates only — meta-agent can deviate.

```python
class BuildToolResult(BaseModel):
    tool_path: Path                   # projects/<p>/tools/<name>/v1/
    files_created: list[str]
    next_steps: list[str]             # e.g. ["fill schemas.py with Input/Output models", "implement handle()", "run eval"]
```

**Sandbox**: `name` validated against pattern + must not collide with existing tool name in the project; `kind_hint` validated against allowed values.

**Refused if**: name collides with a catalog tool (the meta-agent should pin the catalog tool instead, or use a different local name); `kind_hint == "dangerous"` (no such kind).

**Errors**:
- `ConfigError("tool name collides with catalog tool 'X'; use catalog/X@v<N> or pick a different local name")`.
- `ConfigError("invalid kind_hint")`.

The meta-agent then iteratively fills in the scaffold via `write_file`, runs `run_eval`, iterates the handler.

#### `build_function_node(name: str, description: str) -> BuildFunctionNodeResult`

Scaffold a new project-local function node (per `21-agent-system.md` § Function nodes). Creates the 3-file shape:

```
functions/<name>/
├── function.yaml      stub
├── function.py        empty async function
└── README.md          templated
```

Same sandbox rules.

#### `build_connection(name: str, auth_scheme: AuthScheme, target_system: str) -> BuildConnectionResult`

Scaffold a new project-local connection. Creates the connection's 5-file shape under `projects/<scoped_project>/connections/<name>/v1/`.

`target_system` is a free-text hint guiding the scaffold (`"snowflake"`, `"internal_trade_db"`, etc.). The meta-agent uses it to pick reasonable defaults for `config_schema`.

**Refused if**: `auth_scheme == AuthScheme.CUSTOM` without an explicit human flag (per `23-connections-and-auth.md` open question 5). Custom auth schemes require human-written code; meta-agent can't safely invent them.

#### `build_agent(name: str, description: str, model_binding: ModelBinding, output_schema_kind: str) -> BuildAgentResult`

Scaffold a new agent. Creates the agent directory:

```
agents/<name>/
├── agent.yaml         AgentSpec stub
├── prompts/v1.md      prompt skeleton
└── output_schema.py   Pydantic class stub
```

`output_schema_kind` is a hint: `classification`, `extraction`, `report`, `decision`, `freeform`. Affects starter templates for both the prompt and the output Pydantic class.

**Refused if**: `agent.yaml` would set `provider_overrides` (sandbox check on the produced YAML).

#### `new_prompt_version(agent: str) -> NewPromptResult`

Create the next prompt version file in an agent's `prompts/` directory. If the agent currently pins `v3`, produces `v4.md` (initialised by copying `v3.md`'s content). Doesn't change the pin — meta-agent calls `pin_version` separately after editing the new file.

```python
class NewPromptResult(BaseModel):
    new_prompt_path: Path
    new_version: str                  # 'v4'
    parent_prompt_version: str        # 'v3'
```

Why not auto-pin: meta-agent often wants to edit the new file (via `write_file`) before pinning, so the pin reflects intentional content.

#### `pin_version(file: str, key_path: str, new_version: str) -> PinResult`

Update a version pin in a YAML file. Used for:
- Bumping a tool pin in `system.yaml`: `pin_version("system.yaml", "tools.query_snowflake.version", "v3")`.
- Bumping a prompt pin in `agent.yaml`: `pin_version("agents/investigator/agent.yaml", "prompt.version", "v4")` + corresponding update to `prompt.path`.
- Bumping a connection pin in `system.yaml`.

```python
class PinResult(BaseModel):
    file: str
    key_path: str
    old_version: str
    new_version: str
    related_field_updates: dict[str, str]  # e.g. {"prompt.path": "prompts/v4.md"}
```

**Sandbox**: `file` MUST be inside the scoped project. `key_path` MUST address a known pin location (the tool validates against a fixed list of pin-able key paths).

**Errors**:
- `ConfigError("unknown pin key_path")`.
- `RefResolutionError("target version v<N> doesn't exist for ref <ref>")`.

### Eval

#### `run_eval(scope: Literal["tool", "agent", "project"], target: str, eval_spec_path: str | None = None) -> EvalRunResult`

Run an eval. Wraps `foundry eval` programmatically. Returns the typed result.

`target`:
- For `scope: tool`: ref form `local/validate_deltas@v3` or `catalog/query_snowflake@v2`.
- For `scope: agent`: agent name within the scoped project.
- For `scope: project`: project name (must be the scoped project).

`eval_spec_path`:
- For `scope: tool` or `scope: agent`: optional; defaults to the artifact's standalone eval (e.g., `tools/<name>/v<N>/eval.yaml`).
- For `scope: project`: required (the project's eval set under `projects/<p>/evals/`).

**Sandbox**: `target` MUST resolve to artifacts inside the scoped project (or catalog read-only).

**Cost**: counts against `Session.cost_budget` (which is the forge's cost budget). A long-running eval can exhaust the budget; the meta-agent must consider eval cost when planning iterations.

#### `read_eval_results(eval_run_id: str) -> EvalRunResult`

Re-read a stored eval result from the artifact store. Used to compare past iterations without re-running evals.

#### `compare_versions(scope: Literal["tool", "agent", "project"], target: str, refs: list[str]) -> EvalComparison`

Compare the same eval across multiple versions of an artifact (or pin-sets at the project level). Wraps `foundry eval compare`. Returns the typed `EvalComparison` (per `40-eval-harness.md`).

For `scope: project`: `refs` are commit SHAs (or `pin_set_hash` values). For `scope: tool` / `agent`: `refs` are versions (`v1`, `v2`, `v3`).

The meta-agent calls this after every iteration to check whether the change improved the score. Determines whether to commit-and-continue or rollback.

### Versioning

#### `git_commit(files: list[str], message: str) -> CommitResult`

Stage and commit the listed files. Per `51-git-backbone.md` § Atomic multi-file commits.

```python
class CommitResult(BaseModel):
    commit_sha: str
    message: str
    files_committed: list[str]
    audit_entry_id: str               # the appended audit log entry
```

**Sandbox**:
- All files MUST be inside `projects/<scoped_project>/`.
- Current branch MUST be `foundry/<scoped_project>`.
- `message` MUST conform to the conventional format (validated; auto-formatted if the meta-agent provides only a short summary + structured trailer fields via separate args).

**Errors**:
- `GitBackendError` from underlying git ops (uncommitted changes, hook failure, etc.).
- `ConfigError("file outside sandbox")`.
- `ConfigError("invalid message format")`.

The meta-agent typically provides:

```python
git_commit(
    files=["projects/pipeline_recon/agents/investigator/prompts/v4.md",
           "projects/pipeline_recon/agents/investigator/agent.yaml"],
    message=GitCommitMessage(
        type="forge",
        scope="pipeline_recon/agents/investigator",
        summary="prompt v3 → v4",
        body="Strengthened guidance on amendment-timestamp checks.",
        forge_run_id="01JKM4ABCDEF",
        eval_before=0.86,
        eval_after=None,         # set after the next eval; for now leave None
        cluster_id="late_amendment",
    )
)
```

The tool's implementation formats the structured message into the conventional commit text.

#### `git_show(commit_sha: str) -> CommitDetail`

Show the diff for a commit on the project's branch.

```python
class CommitDetail(BaseModel):
    commit_sha: str
    message: str
    timestamp: datetime
    files_changed: list[FileDiff]

class FileDiff(BaseModel):
    path: str
    diff: str           # unified diff
    insertions: int
    deletions: int
```

**Sandbox**: commit MUST be reachable from `foundry/<scoped_project>` branch.

#### `list_versions(target: str | None = None) -> VersionListing`

List versions for a specific artifact OR list recent commits on the project branch.

`target`:
- `None`: list recent commits on the project branch.
- `"tool/<name>"`: list directory versions for the tool.
- `"agent/<name>/prompts"`: list prompt files for the agent.
- `"connection/<name>"`: list directory versions for the connection.

```python
class VersionListing(BaseModel):
    target: str
    kind: Literal["commits", "directory_versions", "file_versions"]
    entries: list[VersionEntry]

class VersionEntry(BaseModel):
    identifier: str         # commit_sha OR version_string
    summary: str
    timestamp: datetime
    eval_score: float | None  # if known
```

#### `rollback(scope: Literal["tool", "prompt", "project"], target: str, to: str) -> RollbackResult`

Roll back an artifact (per `52-rollback-and-audit.md`). Mirrors the CLI's `foundry rollback`.

`scope`:
- `tool`: target = tool ref (`local/validate_deltas`); to = version (`v2`).
- `prompt`: target = agent name (`investigator`); to = prompt version (`v3`).
- `project`: target = project name; to = commit SHA.

```python
class RollbackResult(BaseModel):
    scope: str
    target: str
    from_version: str
    to_version: str
    commit_sha: str
    audit_entry_id: str
    cache_invalidations: list[str]   # affected cache keys
```

**Sandbox**: scope = project requires explicit operator pre-approval; the meta-agent CAN call it but the CLI surfaces a confirmation prompt before the call dispatches in interactive mode. In autonomous mode, meta-agent's reasoning trajectory is recorded; humans review post-hoc.

**Pre-flight checks**: as per `52-rollback-and-audit.md` § Rollback safety guards. Failure raises `RollbackError`; meta-agent reads the error and adjusts.

The meta-agent calls `rollback` when `compare_versions` shows the most recent change regressed.

## Tool registration

Meta-tools are registered with the meta-agent's `ToolRegistry` at meta-agent construction:

```python
# in foundry/configurator/meta_agent.py
def build_meta_tool_registry(scoped_project, framework_root, catalog_roots, projects_root):
    registry = ToolRegistry()
    
    # Filesystem
    registry.register(read_file_tool(scoped_project, framework_root, catalog_roots, projects_root))
    registry.register(write_file_tool(scoped_project, projects_root))
    
    # Discovery
    registry.register(list_catalog_tool(catalog_roots))
    registry.register(list_tools_tool(scoped_project, projects_root, catalog_roots))
    # ...
    
    # Scaffolds
    registry.register(build_tool_tool(scoped_project, projects_root))
    # ...
    
    return registry
```

The meta-agent's `AgentSpec.tools` allowlist is fixed to the meta-tool names. Project agents have their own (different) registries.

## Error semantics

Meta-tool errors are typed `FoundryError` subclasses (per `10-core-framework.md`). The meta-agent's loop reads the error in its next turn and adapts. Common patterns:

| Error | Meta-agent's typical response |
|---|---|
| `ToolNotAllowedError` | shouldn't happen (meta-agent's allowlist is fixed); if it does, halt iteration |
| `ConfigError("path outside sandbox")` | retry with corrected path (within scoped project) |
| `ConfigError("invalid pin key_path")` | retry with the right key_path; consult `list_tools` / `list_agents` for valid paths |
| `ConfigError("tool name collides with catalog")` | use `catalog/<name>@v<N>` instead of building a new local |
| `RefResolutionError("target version doesn't exist")` | use a version that exists per `list_versions` |
| `GitBackendError("dirty working tree")` | shouldn't happen mid-forge; if it does, halt + ask human |
| `GitBackendError("commit message format invalid")` | retry with corrected message via the structured message helper |
| `RollbackError(pre-flight failed)` | adapt the rollback strategy or accept the change |
| `EvalRunError(infrastructure)` | halt iteration; capture trajectory; surface to operator |

The meta-agent prompt (per `60-meta-agent.md`) includes guidance on common error responses.

## Composition with other primitives

Meta-tools are foundry tools. Therefore they:
- Run through the standard `ToolRegistry.dispatch` (allowlist, validation, retries, observability).
- Emit `tool.started` / `tool.completed` events.
- Are subject to per-tool `timeout_s` and `retry_policy`.
- Their outputs are validated against their declared output schemas.
- Failures are typed `FoundryError` subclasses.

The meta-agent's view: it's just an agent calling tools. The tools happen to be filesystem + git + eval + foundry-config tools rather than business-domain tools.

## Failure modes (tool-level)

| Cause | Surfaced as |
|---|---|
| Path outside sandbox (read or write) | `ConfigError` |
| Path canonicalisation reveals symlink escape | `ConfigError("symlink escape detected")` |
| Path is in immutable version directory | `ConfigError("immutable path")` |
| Wrong git branch | `GitBackendError` |
| Underlying git command fails | `GitBackendError` with git stderr in context |
| Eval target unresolvable | `RefResolutionError` |
| Eval harness infrastructure failure | `EvalRunError` (inherits `FoundryError`) |
| Forbidden git operation attempted | `GitBackendError` at meta-tool layer (before subprocess) |
| Catalog write attempted | `ConfigError("catalog writes are human-gated")` |
| `dangerous: true` in build_tool args | `ConfigError("dangerous tools require human authoring")` |

## Invariants

1. **Meta-tools are foundry tools.** Same protocol; same registry; same dispatch.
2. **Sandbox is enforced at the tool layer.** Path / branch / scope checks happen before underlying ops.
3. **Forbidden actions cannot bypass through prompt.** A meta-agent prompt that says "ignore the sandbox" still gets refused by the tool.
4. **Every meta-tool emits structured events.** No silent operations.
5. **Error context is preserved.** Original cause stored in `context["cause_type"]` even after wrapping as `FoundryError`.
6. **Meta-tool output validation.** Every tool's output is Pydantic-validated; misshapen returns are bugs.
7. **Atomic git commits.** Files staged + committed together; partial commits not exposed.

## Test expectations

### Unit (per tool)

For every meta-tool, fixtures and assertions:

1. **Happy path**: valid inputs → expected output shape.
2. **Sandbox enforcement**: path outside scope → `ConfigError`.
3. **Invalid inputs**: malformed args → `ToolInputValidationError`.
4. **Underlying operation failure**: e.g., `write_file` to a read-only filesystem → `IOError` wrapped.

### Contract

1. **Allowlist completeness**: meta-agent's `tools` list in its `AgentSpec` matches exactly the keys in the meta-tool registry.
2. **No leakage**: a project agent's allowlist + dispatcher refuses any meta-tool name (a regular agent calling `git_commit` is refused at dispatch).
3. **Sandbox bypass attempts**: every documented sandbox check fires for the documented bypass attempts (path traversal, symlink, etc.).

### Integration (Phase 6 exit gate)

1. End-to-end forge using all meta-tools: bootstrap calls every scaffold tool; iteration uses `read_file`, `write_file`, `pin_version`, `git_commit`, `run_eval`, `compare_versions`, `rollback` at least once each.
2. Sandbox enforcement: a contrived meta-agent prompt that outputs forbidden tool calls produces sandbox refusals consistently across forge runs.

## Open questions

1. **Tool versioning**: meta-tools themselves don't have explicit versions today (they're framework code; `foundry==1.3.0` defines the meta-tool set). For finer-grained iteration on meta-tools, version them like catalog tools? Lean: defer; a foundry release ships a coherent meta-tool set; finer versioning adds complexity without clear benefit.
2. **Tool composition**: a meta-tool that calls other meta-tools (e.g., a single `propose_iteration` tool that internally calls `compare_versions`, decides, and calls `rollback` if needed). Lean: no — the meta-agent's reasoning IS the composition layer; pulling decision logic into tools makes the meta-agent less interpretable.
3. **`dry_run` mode** for write-side tools (`write_file`, `pin_version`, `git_commit`, `rollback`). Returns "what would happen" without applying. Useful for the meta-agent's interactive mode (operator sees proposed change before approve). Lean: yes; ship `dry_run: bool = false` field on every write-side meta-tool.
4. **Tool result caching**: meta-tools like `list_catalog` are expensive (file scans across catalog roots) and idempotent. Caching them per-forge-run is sensible. Lean: yes; flag `cacheable: true` on appropriate meta-tools.
5. **Cross-tool consistency checks**: when the meta-agent proposes a change spanning multiple tools (`write_file` then `pin_version` then `git_commit`), should there be a transactional wrapper that ensures all-or-nothing? Lean: yes; introduce `transactional_change(operations: list[MetaToolOp]) -> TransactionResult` in v1.1; for v1, the meta-agent's loop handles failure by rollback if needed.
