# 82 — Developer Experience

## Purpose

This doc consolidates the operator-facing surface: the CLI command catalogue, the `foundry.testing` module + `foundry test` CLI for project-local pytest (per memory note from Tier 6 review), the project test layout convention, the review TUI for commits + forge trajectories, editor integration (LSP / JSON Schema), and the onboarding flow for a new operator + a new project.

The principle: **the foundry should feel like ordinary Python development**. Operators use familiar tools (pytest, IDE debuggers, git, standard cloud CLIs) augmented by foundry-specific commands. No bespoke ceremony.

Three load-bearing properties:

1. **Pytest is the testing default for project code.** The foundry ships `foundry.testing` fixtures + a thin `foundry test` CLI wrapper; the actual test runner is pytest.
2. **The CLI is the primary surface.** TUI and notebooks are alternatives, not replacements; everything works from `foundry <subcommand>`.
3. **Editor integration is via standards.** JSON Schema + Pydantic auto-generation feeds VSCode / IntelliJ / etc.; no custom IDE plugin required.

## Module layout

```
src/foundry/
├── cli/
│   ├── __main__.py            python -m foundry
│   ├── _common.py             shared flags, output formats
│   ├── project.py             foundry project new / list / diff / clone
│   ├── catalog.py             foundry catalog list / show / promote
│   ├── run.py                 foundry run
│   ├── serve.py               foundry serve
│   ├── eval.py                foundry eval / eval compare / eval seed / eval capture
│   ├── forge.py               foundry forge / resume / list / cancel / show / replay / trace
│   ├── rollback.py            foundry rollback / versions / diff
│   ├── obs.py                 foundry obs <subcommand> (per 80-observability)
│   ├── connections.py         foundry connections list / health / refresh / describe
│   ├── workers.py             foundry workers list / drain
│   ├── storage.py             foundry storage <subcommand> (per 81)
│   ├── test.py                foundry test (pytest wrapper)
│   ├── validate.py            foundry validate / doctor
│   ├── deploy.py              foundry deploy (per 84-deployment)
│   └── tui/
│       ├── review.py          foundry review (commit + forge trajectory browser)
│       └── _common.py         textual-based shared components
└── testing/
    ├── __init__.py            public fixtures + helpers
    ├── fixtures.py            RunContextFixture, MockConnection, MockProvider, etc.
    ├── state.py               make_state helper, reducer assertions
    └── conftest.py            pytest plugin auto-loading (via foundry test)
```

## CLI catalogue (consolidated reference)

The complete `foundry <subcommand>` surface, grouped by phase of work:

### Project lifecycle

| Command | Purpose | Detail in |
|---|---|---|
| `foundry init --kind institution <name>` | Scaffold a new institution repo | `86-multi-tenancy-and-ip.md` |
| `foundry project new <name>` | Create a new project | `01-architecture-overview.md` § Multi-institution |
| `foundry project list` | List projects in this repo | this doc |
| `foundry project clone <source> <target>` | Fork a project (Phase 5+) | `31-multi-agent-systems.md` |
| `foundry project diff <name> <ref1> <ref2>` | Diff between project commits | `52-rollback-and-audit.md` |
| `foundry validate` | Load all configs; surface errors | `12-config-and-validation.md` |
| `foundry doctor` | Runtime checks (roots, sandbox, secrets reachable, framework-version compat) | this doc |

### Catalog

| Command | Purpose |
|---|---|
| `foundry catalog list [--kind tools/connections/retrievers/agent_templates]` | Browse catalog |
| `foundry catalog show <ref>` | Show artifact details + versions |
| `foundry catalog promote <project>/<kind>/<name>` | Human-gated promotion (per `50-versioning-model.md`) |
| `foundry catalog deprecate <ref>@<version> --reason "..."` | Mark a catalog version as deprecated |

### Local development + iteration

| Command | Purpose |
|---|---|
| `foundry forge <project> --description "..." --eval <path> [...flags]` | Bootstrap or iterate (per `60`) |
| `foundry forge --interactive ...` | Interactive forge with discuss-mode |
| `foundry forge --resume <forge_run_id>` | Resume interrupted forge |
| `foundry forge list` | Recent forge runs |
| `foundry forge show <id>` | Print trajectory artifact |
| `foundry forge trace <id> [--iteration N]` | Pretty-printed reasoning trace (per `62`) |
| `foundry run <project> --input '...'` | One-shot run (CLI) |
| `foundry run <project> --stream` | Streaming variant |
| `foundry test [<project>]` | Project-local pytest wrapper (this doc) |

### Eval + comparison

| Command | Purpose |
|---|---|
| `foundry eval <project> <eval-set>` | Run a project eval |
| `foundry eval tool <ref>@<version>` | Run a tool's standalone eval |
| `foundry eval agent <project> <agent>` | Run an agent eval |
| `foundry eval compare --tool <name> v1 v2 [v3 ...]` | Cross-version tool compare |
| `foundry eval compare --project <name> --pin-set <a> --pin-set <b>` | Cross-pin-set project compare |
| `foundry eval seed --tool <name> --examples <path>` | Bootstrap eval cases (per `60`) |
| `foundry eval capture --project <p> --since <d>` | Seed cases from production traffic (per `41`) |
| `foundry eval matrix --models <m1>,<m2>,...` | Cross-model comparison (per `41`) |
| `foundry eval list <project>` | Recent eval results |
| `foundry eval show <eval_run_id>` | Full per-case details |

### Versioning + rollback

| Command | Purpose |
|---|---|
| `foundry versions <project>` | Recent commits + per-artifact version state |
| `foundry diff <project> <ref1> <ref2> [--path <p>]` | Git-diff-shaped output |
| `foundry rollback <project> --tool <name> --to <version>` | Per-tool rollback |
| `foundry rollback <project> --prompt <agent> --to <version>` | Per-prompt rollback |
| `foundry rollback <project> --to <commit>` | Per-project rollback |
| `foundry rollback ... --dry-run` | Preview |
| `foundry review <project>` | Interactive TUI for commits + rollback (this doc) |

### Connections + health

| Command | Purpose |
|---|---|
| `foundry connections list [--project <p>]` | Bound connections + catalog availability |
| `foundry connections health [<project>]` | Run health checks; exit non-zero on failure |
| `foundry connections describe <project>/<name>` | Show ConnectionDescriptor |
| `foundry connections refresh <project>/<name>` | Force pool refresh |

### Approvals (HITL)

| Command | Purpose |
|---|---|
| `foundry approvals list [<project>]` | Pending approvals |
| `foundry approvals show <run_id> <approval_id>` | Details |
| `foundry approvals approve <run_id> <approval_id> [--reason "..."]` | Resolve |
| `foundry approvals reject <run_id> <approval_id> --reason "..."` | Reject |
| `foundry approvals stats [<project>]` | Counts + median wait time |

### Observability + audit

| Command | Purpose | Detail |
|---|---|---|
| `foundry obs cost [--project] [--since] [--by]` | Cost breakdown | `80-observability.md` |
| `foundry obs latency [--project] [--p50/p95]` | Latency aggregates | `80` |
| `foundry obs failures [--project] [--by error_class]` | Failure distribution | `80` |
| `foundry obs eval-trend [--project] [--since] [--check-regression]` | Drift detection | `41` + `80` |
| `foundry obs trace <run_id>` | Render OTel trace tree | `80` |
| `foundry obs audit <project> [--since] [--type]` | Query audit log | `52` |
| `foundry obs forge [<project>] / <forge_run_id>` | Forge aggregates / details | `62` |
| `foundry obs guards <project>` | Guard findings | `30` |

### Operations

| Command | Purpose |
|---|---|
| `foundry serve <project> [--workers N] [...]` | Launch FastAPI service |
| `foundry workers list` | Live workers + per-worker metrics |
| `foundry workers drain <worker_id>` | Graceful drain |
| `foundry deploy <project> --image <tag> [--pre-deploy-eval] [--production-floor]` | Deploy with gate (per `84`) |
| `foundry batch submit <project> --items <path> [...]` | CLI wrapper for `POST /batch` |
| `foundry batch status <batch_id>` | Batch progress |
| `foundry batch retry-failed <batch_id>` | Resubmit failures |

### Storage

| Command | Purpose | Detail |
|---|---|---|
| `foundry storage stats` | Disk usage by kind | `81-storage-and-artifacts.md` |
| `foundry storage gc --kind <kind> --older-than <duration>` | Garbage collection | `81` |
| `foundry storage archive --kind runs --older-than 90d` | Archive to gzip tarballs | `81` |
| `foundry storage pin <kind> <id>` | Mark for retention | `81` |
| `foundry storage migrate <from> <to>` | Backend migration | `81` |

### Cache

| Command | Purpose | Detail |
|---|---|---|
| `foundry cache evict --tool <name> --project <p>` | Force-evict tool cache | `24-caching-and-optimisation.md` |
| `foundry cache stats --project <p>` | Hit rates per cache layer | `24` |

Output format conventions: tabular by default; `--json` for machine-readable; `-v` for verbose; `-q` for quiet; `--no-color` for non-TTY environments.

## `foundry.testing` module + `foundry test` CLI

Per the Tier 6 review (memory note): pytest is the testing default for project-local code. The foundry ships fixtures + a thin CLI wrapper.

### `foundry.testing` fixtures

```python
# foundry/testing/__init__.py — public surface
from .fixtures import (
    RunContextFixture,
    MockConnection,
    MockConnectionAccessor,
    MockProvider,
    MockEmbedder,
    MockRetriever,
    MockReranker,
    SemanticCacheFixture,
    ResultCacheFixture,
)
from .state import make_state, assert_state_transition, StateBuilder
```

#### `RunContextFixture`

Constructs a mock `RunContext` for testing handler.py and function.py code without spinning the framework:

```python
# tests/test_handlers.py
import pytest
from foundry.testing import RunContextFixture, MockConnection
from projects.pipeline_recon.tools.validate_deltas.v3.handler import handle
from projects.pipeline_recon.tools.validate_deltas.v3.schemas import ValidateIn, ValidateOut

@pytest.mark.asyncio
async def test_validate_within_tolerance():
    ctx = RunContextFixture(
        run_id="test-run",
        agent_name="investigator",
        tool_ref="local/validate_deltas@v3",
        connections=MockConnectionAccessor({
            "reference_db": MockConnection(client=fake_snowflake_client())
        }),
    ).build()
    
    result = await handle(
        ValidateIn(
            trade_id="T1",
            observed_amount=1000.05,
            expected_amount=1000.00,
            tolerance_bp=10,
        ),
        ctx,
    )
    
    assert isinstance(result, ValidateOut)
    assert result.is_within_tolerance is True
    assert result.severity == "ok"
```

Helpers cover all the fields a handler might read from `ctx`. Defaults are sensible for tests.

#### `MockConnection` / `MockConnectionAccessor`

```python
# Drop-in connection mocks for tools that touch external systems.

mock_snowflake = MockConnection(
    client=Mock(spec=SnowflakeConnection),
    descriptor=ConnectionDescriptor(
        ref="catalog/snowflake@v2",
        slot="reference_db",
        auth_scheme=AuthScheme.KEY_PAIR,
        principal="test-service-account",
    ),
)

ctx = RunContextFixture(
    connections=MockConnectionAccessor({"reference_db": mock_snowflake})
).build()
```

`MockConnection` records all calls for assertion. Useful for: "did the handler acquire the right connection slot? Did it call the right method on the client?"

#### `MockProvider`

```python
# Drop-in Provider for testing agents without LLM costs.

mock_provider = MockProvider(
    name="anthropic",
    model="claude-opus-4-7",
    responses=[
        ModelResponse(
            message=FoundryMessage(
                role=MessageRole.ASSISTANT,
                content=[TextBlock(text='{"root_cause": "late_amendment", "confidence": 0.9}')]
            ),
            stop_reason=StopReason.END_TURN,
            usage=TokenUsage(input_tokens=100, output_tokens=20),
            ...
        )
    ],
)

# Use in agent tests via MetaAgent or direct BaseAgent construction
```

Scripted responses enable deterministic agent tests — the test specifies what the LLM "would have" returned; pytest verifies the agent processes it correctly.

#### `make_state` + `StateBuilder`

```python
# Construct a state-model instance for testing function nodes / state transitions
state = make_state(
    spec_path="projects/pipeline_recon/state.yaml",
    messages=[FoundryMessage(role="user", content=[TextBlock(text="...")])],
    current_exception=...,
    detected_breaks=[],
)
```

`make_state` reads the project's `state.yaml`, constructs the Pydantic model, populates with the kwargs. Validates against the schema; raises if shape mismatches.

#### `assert_state_transition`

```python
# Test reducer behaviour
from foundry.testing import assert_state_transition

state_before = make_state(spec_path="...", messages=[m1])
deltas = [
    {"messages": [m2]},   # APPEND reducer
    {"messages": [m3]},
]

assert_state_transition(
    spec_path="...",
    initial=state_before,
    deltas=deltas,
    expected_final={"messages": [m1, m2, m3]},
)
```

### `foundry test` CLI

Wraps pytest with sensible defaults:

```bash
# Run all tests in a project:
foundry test projects/pipeline_recon

# Standard pytest flags forwarded:
foundry test projects/pipeline_recon -k test_validate
foundry test projects/pipeline_recon -x
foundry test projects/pipeline_recon --lf

# Combined with eval gate:
foundry test projects/pipeline_recon --with-eval evals/q1.yaml --fail-under 0.90
```

Defaults that `foundry test` adds:
- Test discovery in `projects/<p>/tests/`.
- Auto-loads `foundry.testing.conftest` plugin (provides fixtures globally without operator setup).
- Sets `PYTHONPATH` to include the project root + framework root.
- Reports test results in a foundry-consistent format alongside eval results.
- Exit codes: 0 (pass), 1 (test failure), 2 (infra failure), 3 (eval failed under threshold) — distinguishable for CI.

### Project test layout convention

```
projects/<name>/
├── tests/
│   ├── conftest.py                    # imports + project-specific fixtures
│   ├── test_handlers.py               # pytest for tool handler.py code
│   ├── test_functions.py              # pytest for function-node bodies
│   ├── test_state_transitions.py      # pytest for state reducer + visibility behaviour
│   ├── test_integration.py            # end-to-end with real or VCR-replayed connections
│   └── fixtures/
│       ├── snowflake_responses.yaml   # recorded responses for replay
│       ├── slack_responses.yaml
│       └── sample_inputs.yaml
└── ...
```

Documented but not enforced. Operators free to deviate; the convention exists so consistency emerges naturally across projects + the foundry's own examples follow it.

### What's pytest, what's eval

Already documented in `40-eval-harness.md` § Eval is behavioural; pytest is code-level. Recap: eval = behavioural contract; pytest = implementation correctness; both needed.

## `foundry review` — interactive TUI

Designed in `52-rollback-and-audit.md` § Review TUI; consolidating the spec here.

```bash
foundry review pipeline_recon
```

Layout:

```
┌─ foundry review pipeline_recon ────────────────────────────────────────────┐
│                                                                            │
│  Recent commits ─────────────────────────────────────────────────────┐    │
│  > f1d1542  forge  prompt: investigator v6→v7   eval +0.02   2h ago  │    │
│    e7914ac  human  pin: query_snowflake v2→v3   eval ±0.00   1d ago  │    │
│    6def016  forge  prompt: resolver v3→v4       eval +0.02   2d ago  │    │
│                                                                       │    │
│  [Tabs] commits / forges / approvals / connections                   │    │
│  Selected: f1d1542 ──────────────────────────────────────────────────┘    │
│                                                                            │
│  Diff:                                                                     │
│  @@ projects/pipeline_recon/agents/investigator/agent.yaml @@             │
│  - version: v6                                                             │
│  + version: v7                                                             │
│                                                                            │
│  Eval context:                                                             │
│    before: 0.91 (run 01JKL...)                                            │
│    after:  0.93 (run 01JKM...)                                            │
│    cluster: late_amendment (cleared 4/5 cases)                            │
│                                                                            │
│  Operator: meta_agent (forge 01JKM4...)                                    │
│  Human supervisor: operator@example.com                              │
│                                                                            │
│  [r] rollback   [d] full diff   [e] open in editor   [t] tabs   [q] quit │
└────────────────────────────────────────────────────────────────────────────┘
```

Key bindings + semantics in `52`. Read-only by default except for the rollback action. Built with `textual` (or similar Python TUI library); cross-platform.

Tabs:
- **commits**: as shown.
- **forges**: forge runs with trajectories; drill into iterations.
- **approvals**: pending approvals; resolve from TUI.
- **connections**: bound connections + healths.

## Editor integration

### JSON Schema for YAML configs

Per `12-config-and-validation.md` § JSON Schema emission, every top-level config schema produces a JSON Schema that operators can wire into their IDE:

```bash
foundry schema emit --output docs/_schemas/
# Produces:
#   docs/_schemas/systemspec.schema.json
#   docs/_schemas/agentspec.schema.json
#   docs/_schemas/statespec.schema.json
#   docs/_schemas/toolspec.schema.json
#   docs/_schemas/connectionspec.schema.json
#   docs/_schemas/evalspec.schema.json
#   docs/_schemas/functionnodespec.schema.json
```

`.vscode/settings.json` (committed in the institution repo template):

```json
{
  "yaml.schemas": {
    "./docs/_schemas/systemspec.schema.json": "**/projects/*/system.yaml",
    "./docs/_schemas/statespec.schema.json": "**/projects/*/state.yaml",
    "./docs/_schemas/agentspec.schema.json": "**/projects/*/agents/*/agent.yaml",
    "./docs/_schemas/toolspec.schema.json": "**/tools/*/v*/tool.yaml",
    "./docs/_schemas/connectionspec.schema.json": "**/connections/*/v*/connection.yaml",
    "./docs/_schemas/evalspec.schema.json": "**/evals/*.yaml",
    "./docs/_schemas/functionnodespec.schema.json": "**/functions/*/function.yaml"
  }
}
```

Editing YAML in VS Code now gets autocomplete, validation, hover documentation. Standard YAML LSP features.

JetBrains IDEs: same JSON Schema files; configured in JSON Schema Mappings.

### Python type hints

Operator-authored Python (handler.py, function.py, output_schema.py) uses standard Pydantic + foundry-imported types. IDEs render type hints natively. No custom plugin.

### Example snippets

`docs/examples/` — ready-to-copy snippet files:
- `examples/agent.yaml.snippet` — minimal AgentSpec.
- `examples/tool.yaml.snippet` — minimal ToolSpec.
- `examples/handler.py.snippet` — minimal handler.
- `examples/function.py.snippet` — minimal function node.
- `examples/eval.yaml.snippet` — minimal EvalSpec.

VS Code snippets file (`.vscode/foundry.code-snippets`) imports these. Operators expand `agent` + Tab to scaffold a new agent.

## Onboarding flow

For a new operator joining an institution that already uses the foundry:

```bash
# 1. Clone the institution repo:
git clone git@github.com:<institution>/foundry-institution.git
cd foundry-institution

# 2. Install dependencies:
uv sync                              # reads pyproject.toml; installs foundry + project deps

# 3. Run doctor to verify environment:
foundry doctor

# Output:
#   ✓ foundry framework: 1.3.0
#   ✓ Python: 3.12.x
#   ✓ catalog roots: /opt/foundry/catalog/public, /repo/catalog
#   ✓ projects root: /repo/projects
#   ✓ secrets provider: env (development)
#   ⚠ FOUNDRY_TRACING not set (will use otel-console; OK for dev)
#   ⚠ no Postgres checkpointer; will use SQLite (OK for single-host)
#   ✓ all configs load; 4 projects found

# 4. List projects:
foundry project list

# 5. Smoke-test a project:
foundry run pipeline_recon --input '{"trade_id":"TEST","mismatch_usd":100}'

# 6. Run the project's tests:
foundry test projects/pipeline_recon

# 7. Run the project's eval:
foundry eval pipeline_recon evals/q1.yaml

# 8. Review recent activity:
foundry obs audit pipeline_recon --since 7d
foundry review pipeline_recon
```

For starting a new project in the same institution:

```bash
foundry project new my_new_project

# Now describe the use case + run forge:
foundry forge my_new_project \
  --description "..." \
  --eval projects/my_new_project/evals/q1.yaml \
  --threshold 0.90
```

## `foundry doctor` checks

Self-diagnostic tool the operator runs when something feels off:

```
foundry doctor [--verbose]

Checks (in order):
  ✓ Framework version installed and importable
  ✓ Python version >= 3.12
  ✓ Catalog roots resolve and contain expected structure
  ✓ Projects root resolves
  ✓ Each project: configs load via foundry validate
  ✓ Secrets provider configured + reachable
  ✓ Checkpointer reachable (SQLite always; Postgres if configured)
  ✓ Rate limiter reachable (in-process always; Redis if configured)
  ✓ Audit store writable (or warns if read-only)
  ✓ Observability transport (OTel collector reachable if configured)
  ✓ Storage backend reachable (S3 / Azure / GCS if configured)
  ⚠ Optional services (LangSmith, Langfuse) — opt-in; warns if configured-but-unreachable
  ✓ Sandbox: meta-agent write paths properly scoped
  ✓ Branch state on each project (no detached HEAD; expected branch checked out)
```

Exit code: 0 on all green; non-zero on warnings (configurable strict mode); 2 on hard failures. Useful in CI as a pre-flight check.

## Notebook ergonomics

Per `62-configurator-sessions.md` § Notebook ergonomics. Recap:

```python
from foundry import MetaAgent, CompiledSystem

# Forge from notebook:
agent = MetaAgent.for_project("pipeline_recon")
result = await agent.forge(description="...")
result.summary_dataframe()                # pandas DataFrame
result.eval_progression_plot()            # matplotlib

# Run a project from notebook:
compiled = await CompiledSystem.for_project("pipeline_recon")
run_result = await compiled.run(input={...})

# Inspect:
run_result.events_dataframe()             # all RunEvents as DataFrame
run_result.timeline_plot()                # gantt-style timeline of nodes/tools
```

Notebook helpers live in `foundry[notebook]` optional dep (pulls in pandas + matplotlib). Core foundry doesn't depend on these.

## Failure modes (CLI + UX)

| Cause | Surfaced |
|---|---|
| Command in wrong directory | clear error: "no projects/ found; cd to repo root or pass --root" |
| Unknown project name | error + list of available projects |
| Network error during forge / eval | clear stderr + `foundry forge --resume <id>` hint |
| Operator on wrong git branch | clear error + offer to switch (interactive only) |
| Pre-flight check fails | actionable error message naming what to fix |
| Missing dependency (e.g., textual not installed) | "for `foundry review`, install with `uv pip install foundry[tui]`" |

## Invariants

1. **Every CLI subcommand has stable exit codes** documented per command.
2. **Every CLI subcommand has `--json` for machine-readable output**.
3. **Pytest is the testing primitive**; foundry test is a thin wrapper.
4. **JSON Schema for YAML configs is auto-generated**; consumed by editors.
5. **`foundry doctor` is the operator's self-diagnostic surface** — always available, always actionable.
6. **Read-only commands work without auth on local dev**; only writes require auth in prod.

## Test expectations

### Unit

1. **CLI argument parsing**: every subcommand accepts documented flag combinations; missing required produces clear error.
2. **`foundry test` plugin loading**: pytest sees `foundry.testing` fixtures without operator-authored conftest changes.
3. **Mock fixtures**: each `MockConnection` / `MockProvider` / etc. behaves as documented when used in pytest tests.
4. **JSON Schema generation**: each top-level schema produces a valid JSON Schema (draft-2020-12).

### Contract

1. **Stable exit codes**: every CLI subcommand documents + tests its exit-code contract.
2. **`--json` output is parseable** for every command (CI snippet validates).
3. **Schemas in `.vscode/settings.json` template match the emitted schemas** (consistency check).

### Integration (Phase 9 exit gate)

1. End-to-end onboarding flow: `git clone` + `uv sync` + `foundry doctor` + `foundry run` + `foundry test` + `foundry eval` works from a clean machine in <10 minutes.
2. Review TUI: launch on a project with 20+ commits; navigation works; rollback action commits correctly.
3. `foundry test --with-eval`: combined gate runs tests AND eval; exits non-zero on either failure with distinguishable codes.

## Open questions

1. **Shell completion** (bash / zsh / fish). `foundry --completion zsh > ~/.zsh/completions/_foundry`. Lean: yes, ship; one-time auto-generated from CLI definitions; cheap.
2. **`foundry repl`** — Python REPL with foundry pre-imported and a project pre-loaded. Useful for poking. Lean: yes, simple ipython wrapper.
3. **VS Code extension** — beyond JSON Schema, a real extension with command palette + status-bar integration. Lean: defer to v1.1+; institutions can build if needed.
4. **Web UI** for ops dashboards — separate from review TUI. Lean: defer; OTel backends + their UIs cover ops dashboards.
5. **Tab completion across projects** in CLI args. Lean: yes, via shell completion (item 1).
