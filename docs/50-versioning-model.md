# 50 — Versioning Model

## Purpose

This doc defines what is versioned in the foundry, how versions are named, how they evolve, and how compile-time content hashes relate to disk-versioned artifacts. It establishes the discipline that makes per-artifact rollback (`52-rollback-and-audit.md`) trustworthy and project reproducibility (`31-multi-agent-systems.md`) tight.

The **three-axis versioning model** was introduced in Tier 0 (`00-vision-and-scope.md`, `01-architecture-overview.md`). This doc is the consolidating spec — full enumeration of what's versioned on which axis, schema evolution rules per artifact kind, catalog semver discipline (the resolved open question from `00`), framework-version compatibility, and migration patterns.

Three load-bearing properties:

1. **Three independent axes, each with appropriate granularity.** Tools and prompts get directory- and file-level explicit versions for human legibility + per-artifact rollback; everything else relies on git for diff-based history.
2. **Content hashes for compositions.** `system_version`, `agent_version`, `eval_spec_hash` are content-hashed over the full transitive config. Same content → same hash, deterministically. Used for cache invalidation, reproducibility, and version-comparison shortcuts.
3. **Schema evolution is bounded.** Additive changes are always safe (defaults provided). Breaking changes require explicit `schema_version` bumps + migrations. Catalog promotion warns on schema-incompatible bumps.

## The three axes (full enumeration)

### Axis 1: Directory-versioned (`v<N>/`)

Artifacts that are **shareable, standalone, immutable once committed**. Each version is a frozen directory; bumping creates `v<N+1>/`. The original lives on disk forever (rollback target).

| Artifact | Path |
|---|---|
| Catalog tools | `catalog/tools/<name>/v<N>/` |
| Project-local tools | `projects/<p>/tools/<name>/v<N>/` |
| Catalog connections | `catalog/connections/<name>/v<N>/` |
| Project-local connections | `projects/<p>/connections/<name>/v<N>/` |
| Catalog retrievers | `catalog/retrievers/<name>/v<N>/` |
| Catalog agent templates | `catalog/agent_templates/<name>/v<N>/` |

The `versions.json` file at the parent directory tracks metadata: which versions exist, their eval scores, deprecation status, author, created_at.

Pinned by:
- Tool / connection / retriever versions: `SystemSpec.tools[name].version`, `SystemSpec.connections[name].version`, `AgentSpec.retrievers[].version`.
- Agent templates: only used as scaffold sources, not pinned at runtime.

### Axis 2: File-versioned (`v<N>.md`)

Artifacts that are **content-only, frequently iterated, legibility-first**. Each version is a numbered file in a directory; the live version is pinned in the consuming config.

| Artifact | Path |
|---|---|
| Agent prompts | `projects/<p>/agents/<a>/prompts/v<N>.md` |
| Memory consolidator prompts | `projects/<p>/agents/<a>/prompts/<consolidator_name>_v<N>.md` (convention; not enforced) |

Pinned by:
- Agent prompts: `AgentSpec.prompt.version`.
- Memory consolidator prompts: `MemoryConfig.layers[].consolidator_prompt` (path includes `v<N>` by convention).

The numeric ordering carries semantic meaning: `v3` is meant to be "iteration 3 of this prompt" — contiguous, no gaps, no skipping.

### Axis 3: Git-versioned (single files, history via commits)

Everything else. Lives as a single file in the project; history is git's job. Diffs visible via `git diff` / `foundry diff`.

| Artifact | Path |
|---|---|
| `system.yaml` (the manifest) | `projects/<p>/system.yaml` |
| `state.yaml` (state schema + visibility) | `projects/<p>/state.yaml` |
| `agent.yaml` (per-agent config; pins prompt version) | `projects/<p>/agents/<a>/agent.yaml` |
| `output_schema.py` (per-agent Pydantic output model) | `projects/<p>/agents/<a>/output_schema.py` |
| `function.yaml` + `function.py` (per function-node config + body) | `projects/<p>/functions/<n>/{function.yaml, function.py}` |
| Eval specs | `projects/<p>/evals/<name>.yaml` |
| Catalog index | `catalog/index.yaml` |
| `versions.json` metadata files | `<root>/<artifact>/<name>/versions.json` |
| Audit log | `projects/<p>/.foundry/audit.jsonl` |
| Project READMEs, deploy manifests, etc. | wherever they live |

Pinned by: nothing (these ARE the pin records). Rollback uses `git checkout <ref> -- <path>`.

## Why three axes (rationale recap)

A single axis (just git) would mean: rolling back one tool requires committing a file edit that changes the pin in `system.yaml`; the tool's own files are unchanged. That's awkward — you can't `ls` to see versions, and the meta-agent's `build_tool` would have to construct PRs rather than write directories.

A single axis (just directories) would mean: every change to `system.yaml` requires a new directory. Pin changes (the most common edit) become heavy.

Three axes match how things are actually used: tools and prompts iterate in immutable steps because they're shareable and reviewed; configs that compose those iterate continuously and benefit from git's diff history.

## Naming conventions (normative)

### Directory versions

- `v` + integer: `v1`, `v2`, `v3`, ..., `v99`, `v100`. Zero-padded NOT required. Maximum 4 digits enforced (a project hitting `v10000` should refactor).
- No skipping: `v1`, then `v2` (not `v1`, then `v3`). The compiler scans `v<N>/` directories and enforces contiguity at promotion time.
- Within a directory, `v<N>/` files are immutable. Editing requires `v<N+1>/`.

### File versions

- `v<N>.md` for prompts: `v1.md`, `v2.md`, ...
- `<descriptor>_v<N>.md` for non-default prompts (consolidators, repair prompts): `consolidate_v1.md`, `repair_v2.md`. Convention: descriptor first, then `_v<N>.md`.

### Content hashes

Used in computed identities:
- `system_version`: 16-char hex sha256 prefix over the full system content (`31-multi-agent-systems.md`).
- `agent_version`: 16-char hex prefix over agent config + pinned prompt + output schema (`21-agent-system.md`).
- `eval_spec_hash`: 16-char hex prefix over eval spec content (`40-eval-harness.md`).
- `pin_set_hash`: 16-char hex prefix over the pin set only (versions, not file contents) — used by `foundry eval compare --pin-set` for cheap configuration identity.

16 chars (64 bits) is sufficient uniqueness for the foundry's scope (a single user, dozens of projects, hundreds of versions). Collision probability is astronomically low.

## ArtifactRef (canonical form)

```
<scope>/<kind?>/<name>@<version>
```

Examples:

```
catalog/query_snowflake@v2
local/validate_deltas@v3
catalog/snowflake@v2                   # connection
catalog/agent_templates/router@v1
catalog/connections/pgvector@v1
local/connections/internal_api@v3
```

Parse rules:
- `<scope>` is `catalog` or `local`.
- `<kind>` is optional and defaults to `tool`. Other values: `connection`, `retriever`, `agent_template`. When kind is non-tool, it's required after the scope.
- `<name>` is `[a-z][a-z0-9_-]{0,63}`.
- `<version>` is `v<N>`.

Resolution (per `12-config-and-validation.md`):
1. `<scope>` determines root: `local` → `projects_root/<project>/`; `catalog` → walks `catalog_roots` left-to-right.
2. `<kind>` determines subdirectory: `tools/`, `connections/`, `retrievers/`, `agent_templates/`.
3. `<name>` is the directory name.
4. `<version>` is the directory name.

Failures: `RefResolutionError` with the resolved path and a list of available versions if name exists but version doesn't.

## Schema evolution

Every Pydantic schema in the foundry has a `schema_version: Literal[N]` field. The current `N` is `1`; bumps reflect breaking changes.

### Additive changes (no bump needed)

These are always safe:
- Adding an optional field with a default.
- Adding a new enum value (existing configs that don't use it are unaffected; consumers must handle "unknown enum" gracefully — already required).
- Loosening a constraint (raising a max, lowering a min).
- Documentation-only changes (`description: ...`).

### Breaking changes (require schema_version bump + migration)

These require explicit version bump:
- Removing a field.
- Renaming a field.
- Changing a field's type (incompatible).
- Tightening a constraint (raising a min, lowering a max) such that valid old configs become invalid.
- Removing an enum value.
- Changing the meaning of a field.

When `schema_version` bumps from `N` to `N+1`:
- Old configs (`schema_version: N`) MUST still load — the loader runs migrations.
- Migrations live in `foundry/config/migrations.py` as pure functions: `migrate_v1_to_v2(config: dict) -> dict`.
- Migration is one-step at a time; `v1 → v3` runs `v1_to_v2` then `v2_to_v3`.
- Migrations are idempotent: running them on already-migrated content is a no-op.
- The meta-agent always writes the current `schema_version`.

Migrations are kept forever. Removing a migration breaks reproducibility for older artifact-stores and old git history.

### Per-artifact-kind evolution rules

| Artifact | Evolution discipline |
|---|---|
| `ToolSpec` | Adding optional fields, new tags, new `connections_required` slots are non-breaking. Removing slots, changing `input_schema` / `output_schema` shape are breaking — require new tool version (`v<N+1>/`), not `schema_version` bump (the spec schema didn't change; the tool's contract did). |
| `ConnectionSpec` | Same as ToolSpec. Auth-scheme changes are major: a new `auth_scheme` value requires a new connection version. |
| `AgentSpec` | Adding optional fields (semantic_cache, retrievers, memory) was additive. Renaming or removing fields is breaking — bump `schema_version`. |
| `StateSpec` | Adding fields to `schema:` is additive (existing checkpoints load with default for the new field). Removing or renaming fields breaks checkpoint compatibility — bump `schema_version` AND provide checkpoint migration (rare but real). |
| `SystemSpec` | Same as AgentSpec. |
| `EvalSpec` | Adding scorers, cases, scorer kinds is additive. Renaming `expected` shape is breaking. |
| `MemoryConfig` | Adding layer kinds is additive. Renaming kind enum values is breaking. |
| `FunctionNodeSpec` | Adding optional fields additive. Function signature change is breaking. |

### Catalog promotion + semver discipline (resolved open question)

Locked decision (per `00-vision-and-scope.md`, 2026-04-25): **`foundry catalog promote` warns on schema-breaking promotions; `--strict-semver` blocks them.**

How "schema-breaking" is detected:

For tool/connection/retriever promotion (catalog axis 1):
1. The artifact being promoted has version `v<N>`. The catalog has prior versions `v1`, ..., `v<N-1>`.
2. Compare the new version's `input_schema` / `output_schema` (for tools) or `config_schema` (for connections) to the immediately-prior version's.
3. If a field was removed, renamed, or had its type changed → schema-breaking.
4. Otherwise → schema-compatible.

```
$ foundry catalog promote pipeline_recon/tool/validate_deltas
WARNING: Schema-breaking change vs catalog/validate_deltas@v2:
  - removed field: tolerance_pct
  - renamed field: amount → observed_amount
This will produce v3. Existing projects pinning @v2 will continue to work,
but bumping their pin to @v3 will require config edits.
Promote anyway? [y/N]

$ foundry catalog promote pipeline_recon/tool/validate_deltas --strict-semver
ERROR: Schema-breaking change vs catalog/validate_deltas@v2 (see warning above).
Use --allow-breaking to override.
```

The warning is required reading. The catalog's `versions.json` records the breaking-change classification:

```json
{
  "versions": [
    {"version": "v1", "schema_change": "initial", "eval_score": 0.91},
    {"version": "v2", "schema_change": "additive", "eval_score": 0.93},
    {"version": "v3", "schema_change": "breaking", "eval_score": 0.95,
     "breaking_changes": ["removed: tolerance_pct", "renamed: amount → observed_amount"]}
  ]
}
```

Consumers can query: `foundry catalog show validate_deltas` shows the `versions.json` with breaking-change annotations, helping operators decide which version to pin.

## Project versioning vs framework versioning

Two distinct versioning concerns:

### Project version (`system_version`)

Per `31-multi-agent-systems.md`: content hash over the project's full config + pinned files. Changes when ANY config or pinned file changes. Surfaces in:
- `RunStarted` event.
- Audit log entries.
- Eval-result artifact metadata.

This is what makes a project run reproducible.

### Foundry framework version

The `foundry` Python package itself has a version (`foundry.__version__`, e.g. `1.3.0`). Two implications:

1. **Cross-version compatibility**: configs that loaded with `foundry==1.2.0` may load differently with `foundry==1.3.0` if migrations exist. The loader records `framework_version` in the run artifact so anomalies are traceable.
2. **Pin in consumers**: institution repos pin `foundry==1.3.0` exactly in their `pyproject.toml` (per `86-multi-tenancy-and-ip.md`). Bumps are deliberate; CI re-runs evals against the new framework before any production deploy.

### Compatibility matrix

The framework guarantees:
- **Patch-level changes** (`1.3.0 → 1.3.1`) — pure bug fixes, no schema or behavioural changes.
- **Minor-level changes** (`1.3.0 → 1.4.0`) — additive features, no schema bumps.
- **Major-level changes** (`1.3.0 → 2.0.0`) — schema bumps possible; explicit migration guide ships.

If the framework requires a schema migration, loading an old config emits a one-time warning per process explaining the migration was applied; the migrated config is held in memory but NOT written back to disk (writing back is a deliberate `foundry config migrate` operation).

## Versioning interactions

### With caching

- **Semantic cache**: keyed on `agent_version`. Bumping any pinned prompt / tool version / model setting changes `agent_version`, invalidating cached entries for that agent. Per `24-caching-and-optimisation.md` § Correctness rules.
- **Tool-result cache**: keyed on `(tool_ref, tool_version, input_hash)`. Bumping a tool version creates a separate cache namespace; old entries remain accessible if a project rolls back.
- **Prompt-cache (provider-native)**: keyed on prompt prefix bytes; a prompt edit invalidates the provider-side cache. No foundry intervention needed.

### With checkpointing

- Checkpoints are tied to `run_id`, not `system_version`. A run started against `system_version: A` continues against `A` even if pins are bumped mid-run. New runs after the bump use the new version.
- For long-running runs that span schema migrations: in-flight checkpoints continue with their original schema; new runs get the new schema. No cross-schema migration of in-flight runs in v1 (cancel + restart with the new config).

### With evals

- `eval_spec_hash` is over the eval spec content. `compare` operations require equal `eval_spec_hash` — different spec hashes can't be compared directly.
- `EvalRunResult.target_version` records what was evaluated; allows querying "all evals against `pipeline_recon@<sha>`."
- Eval results are immutable artifacts; modifying an eval spec creates a new `eval_spec_hash` and old results remain queryable but flagged as different-spec.

### With promotion

- `foundry catalog promote` is the only operation that creates new catalog versions.
- Promotion runs the artifact's standalone eval against a configurable floor (default 0.85). Refuses below floor.
- Promotion bumps catalog version `v<N> → v<N+1>` (contiguous).
- Promotion records the promoting human's identity (from auth context or git config) in `versions.json`.

## Versioning failure modes

| Cause | Surfaced as |
|---|---|
| Non-contiguous version numbering (`v1`, `v3` exists, no `v2`) | `ConfigError` at registry load |
| Pinned version that doesn't exist on disk | `RefResolutionError` |
| Schema mismatch (config has `schema_version: 99` unknown to framework) | `ConfigLoadError` with required framework version range |
| Migration crashes (old config can't be migrated forward) | `ConfigError` with the failed migration step |
| Promotion of artifact that fails its standalone eval | `CatalogPromotionRefused` with the eval result |
| Two project artifacts share a name across the agent/function namespaces | `CompileError("namespace collision")` per `30-orchestration-patterns.md` |
| Catalog versions.json out of sync with directory contents | `ConfigError` at catalog index load |

Every failure mode raises before any execution; no run starts against an inconsistent versioning state.

## Migration patterns (operator workflows)

### Bumping a catalog tool across all projects

```bash
$ foundry catalog list-consumers query_snowflake
projects/pipeline_recon  pinned at v2
projects/contract_review pinned at v2
projects/support_triage  pinned at v1

$ foundry catalog promote ...                  # produces v3, additive change
$ foundry catalog list-consumers query_snowflake
projects/pipeline_recon  pinned at v2  (new: v3 available, additive)
projects/contract_review pinned at v2  (new: v3 available, additive)
projects/support_triage  pinned at v1  (new: v3 available, additive)
```

Each project chooses when to bump; opt-in, deliberate. There is no "auto-upgrade pin" command — bumps are commits in the project repo with eval-driven validation.

### Migrating an in-flight project across foundry framework versions

```bash
$ foundry config migrate projects/pipeline_recon
Detected schema_version 1 in 3 files; framework supports schema_version 2.
Will apply migrations:
  - state.yaml: v1 → v2 (renames `messages` → `conversation_messages`)
  - agent.yaml × 3: v1 → v2 (no changes; revalidation only)

Dry-run output (no files written). Run with --apply to write changes.

$ foundry config migrate projects/pipeline_recon --apply
Applied 4 migrations.
Run `foundry eval pipeline_recon evals/q1.yaml` to validate behaviour preserved.
```

Migration is opt-in and explicit. Operators control timing.

### Retiring an artifact version

```bash
$ foundry catalog deprecate query_snowflake@v1 \
    --reason "Use v2+; v1 had a SQL injection vulnerability fixed in v2"
```

Updates `versions.json` to mark `v1` deprecated. Future reads of `v1` emit a startup warning ("deprecated; reason: ..."). Existing pins continue to work; promotion of new versions warns on consumer projects still pinned to deprecated versions.

## Invariants

1. **No version number is reused.** Once `tool/v3` exists, no future commit may rewrite `v3`'s contents. Enforced at promotion + at meta-agent's `build_tool`.
2. **Version numbering is contiguous.** No gaps in `v1`, `v2`, ...; loader fails on holes.
3. **Pinned version must exist on disk.** Compile fails on unresolvable pins.
4. **`schema_version` bumps require migrations.** Removed migrations break reproducibility — they are kept forever.
5. **Content hashes are deterministic.** Same content → same hash; cross-process reproducible.
6. **Catalog promotion is the only path that creates catalog versions.** Direct git commits to `catalog/<artifact>/v<N>/` are forbidden by repo conventions (the meta-agent's sandbox forbids; humans should respect).
7. **Project artifact removals are soft.** Renaming a tool from `local/foo` to `local/bar` doesn't delete `local/foo/v<N>/` — the versions remain accessible for rollback. Cleanup is a separate, deliberate operation.

## Test expectations

### Unit

1. **ArtifactRef parse round-trip**: every valid ref form parses and re-serialises identically.
2. **Version contiguity check**: a directory with `v1`, `v3` (missing `v2`) fails registry load with a clear error.
3. **Migration application**: a v1 fixture config + v1→v2 migration → loads as v2 successfully; idempotent on re-application.
4. **Content-hash determinism**: the same `SystemSpec` content produces the same `system_version` across processes.
5. **Pin resolution failure**: pin `validate_deltas@v99` against a project where `v99/` doesn't exist → `RefResolutionError` with available-versions list.
6. **Catalog promotion semver detection**: a tool with a removed field promoted vs a prior version → warning; `--strict-semver` → block.
7. **Schema_version unknown**: config with `schema_version: 999` → `ConfigLoadError` naming the supported range.

### Contract

1. **Migration completeness**: every `schema_version` bump in the framework has a corresponding migration in `foundry.config.migrations`.
2. **Migration is forever**: removing a migration produces a CI failure (a fixture-load test holds old-version configs).
3. **`pin_set_hash` cheap independence**: changing only file content (not pins) keeps `pin_set_hash` constant; changes pin → changes both `pin_set_hash` and `system_version`.

### Integration (Phase 5 exit gate)

1. End-to-end: bumping a tool from `v2` → `v3` (additive change) and re-running a project eval shows version-pinned correctness.
2. Schema migration: simulated v1 → v2 migration of `state.yaml`; project loads cleanly under both schemas via the migration.
3. Catalog promotion blocked: an artifact failing its standalone eval refused for promotion.
4. Catalog promotion warns: schema-breaking promotion warns and records `breaking_changes` in `versions.json`.

## Open questions

1. **Rich semver (vs integer-only)**. Currently `v<N>` integers. Real semver (`v1.2.3`) would let patch-level changes (eval threshold tweak, doc fix) skip the full review needed for minor/major bumps. Lean: defer; integer is simpler and the operational overhead of "yet another bump" is small. Revisit if catalog grows past 50 tools and operators feel the need.
2. **Auto-upgrade pins on additive changes**. A "safe upgrade" command that bumps all consumers to the latest catalog version when the change is purely additive (no schema break). Lean: yes, ship as `foundry catalog upgrade-additive --project <name>` but with explicit confirmation per artifact; useful but not core. Phase 5 polish.
3. **Cross-project artifact garbage collection**. When a project deletes a tool (no longer pinned anywhere), the directory remains. Stale directory cleanup (`foundry project cleanup-unused`) is useful for repo hygiene but not safety-critical. Lean: defer; document the manual cleanup pattern.
4. **Per-version operator notes in versions.json**. Allow operators to attach a free-text "why this version exists" alongside eval scores. Useful for catalog discoverability. Lean: yes, `notes:` field on `VersionMetadata`; promotion command prompts for it.
5. **Schema introspection CLI**. `foundry schema show ToolSpec --version 1` to dump the JSON Schema for a given foundry-framework version. Useful for IDE setup + operator documentation. Lean: yes, Phase 9 dev-UX.
