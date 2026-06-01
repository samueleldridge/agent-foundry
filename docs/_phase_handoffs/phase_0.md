# Phase 0 handoff — repo skeleton

**Session date:** 2026-06-01
**Branch:** `main`
**Status:** Phase 0 complete; ready for review.

## What this session built

A repo skeleton ready to accept Phase 1 code:

- `pyproject.toml` with pinned runtime + dev dependencies (Python 3.12,
  `uv`-managed; rationale embedded as a leading comment block).
- `uv.lock` generated from a fully clean `.venv` (deleted and re-synced
  twice during this session; reproducible).
- `.python-version` set to `3.12` (already present at session start; left
  unchanged).
- `src/foundry/` with **19 subpackages**, each having a placeholder
  `__init__.py` plus the sub-files named in `docs/01-architecture-overview.md`
  § Directory layout. Every file is a docstring-only stub.
- `ruff.toml` (root) + `src/foundry/core/ruff.toml` (nested) enforcing the
  import-boundary rules from `docs/10-core-framework.md` § Enforcement.
- `pytest` config in `pyproject.toml` (`testpaths`, `asyncio_mode = "auto"`,
  three markers).
- `tests/{unit,integration,contract}/` with `.gitkeep`s; `tests/unit/test_smoke.py`
  asserts `import foundry` plus per-subpackage import success.
- `src/foundry/cli/__main__.py` + `src/foundry/__main__.py` for
  `python -m foundry --help`. Subcommands are registered as stubs that exit
  with a "lands in Phase N" message.
- `catalog/` and `projects/` top-level directories with `.gitkeep` and a
  README each (access rules, where artifacts live, multi-institution
  overlay reference).

## Pinned dependency versions

Chosen 2026-06-01 by resolving against a clean `uv init`-scratch project
with Python 3.12, then carrying the versions verbatim into our
`pyproject.toml`. uv.lock is the source of truth for transitive pins; the
direct pins below are the contract.

| Package | Pin in `pyproject.toml` | Resolved (uv.lock at session end) | Why |
|---|---|---|---|
| `pydantic` | `>=2.13,<3` | `2.13.4` | v2 is the validation backbone (`CLAUDE.md` invariant); cap below v3. |
| `langgraph` | `==1.2.2` | `1.2.2` | docs/03 specifies an exact pin. Minor releases have broken adapters before — Risk register item 1. |
| `langchain-core` | `==1.4.0` | `1.4.0` | Exact pin; co-version with langgraph 1.2.x. |
| `langchain-anthropic` | `>=1.4,<2` | `1.4.4` | Tracks langchain-core 1.x. |
| `langchain-openai` | `>=1.2,<2` | `1.2.2` | Tracks langchain-core 1.x. |
| `pyyaml` | `>=6.0` | `6.0.3` | YAML config loader. |
| `structlog` | `>=25.5` | `25.5.0` | Structured logs with `run_id` (CLAUDE.md invariant). Chosen over `loguru` for typed key/value contexts. |
| `typer` | `>=0.26` | `0.26.4` | CLI framework (Phase 0 deliverable list). Chosen over argparse for typed signatures and over click for the slimmer decorator surface. |
| `anyio` | `>=4.13` | `4.13.0` | Async primitives + cancellation scopes for `Session` (docs/10). |
| `httpx` | `>=0.28` | `0.28.1` | Async HTTP for providers. |
| `opentelemetry-api` | `==1.42.1` | `1.42.1` | OTel always-on (CLAUDE.md invariant). |
| `opentelemetry-sdk` | `==1.42.1` | `1.42.1` | All three OTel packages move together. |
| `opentelemetry-exporter-otlp` | `==1.42.1` | `1.42.1` | Matching exact pin. |
| `ruff` (dev) | `>=0.15.15` | `0.15.15` | Lint + `flake8-tidy-imports` for boundary enforcement. |
| `pytest` (dev) | `>=9.0` | `9.0.3` | Test runner. |
| `pytest-asyncio` (dev) | `>=1.4` | `1.4.0` | `asyncio_mode=auto`. |
| `mypy` (dev) | `>=2.1` | `2.1.0` | `mypy --strict` is part of every phase's DoD. |

Rationale for the mixed pin style: exact `==` only where docs/03 named it
("langgraph ==<exact>", "langchain-core ==<exact>") and for the OTel triple
(must move together). Everything else uses an upper bound to allow patch
upgrades; the lockfile pins transitive deps exactly. This matches the
project policy that `uv.lock` is committed.

## CLI framework choice

Picked **Typer** over argparse and click. Documented in `pyproject.toml`'s
leading comment block.

- Typer gives typed function signatures and rich `--help` output for free.
- It is already on the Phase 0 dependency list (docs/03), so adopting it
  costs nothing extra.
- Per-command help strings are visible via `python -m foundry --help` and
  per-command `--help` works for every subcommand stub.

## Import-boundary lint structure

The original idea — a single `ruff.toml` with per-file-ignores that
selectively exempts each non-core directory from the foundry-internal bans
— turned out to be wrong. `TID251` is rule-level all-or-nothing: an ignore
for "foundry.providers" in `src/foundry/api/` would also exempt the
"anthropic" ban in the same path.

Solution shipped: split the config across two files.

- **Root `ruff.toml`** holds the third-party SDK bans
  (`langgraph`, `langchain_core`, `langchain_anthropic`, `langchain_openai`,
  `anthropic`, `openai`) with a narrow per-file allowlist for the adapter
  modules (`foundry/runtime/langgraph_adapter.py`,
  `foundry/runtime/_langgraph_types.py`, `foundry/providers/anthropic.py`,
  `foundry/providers/openai.py`).
- **Nested `src/foundry/core/ruff.toml`** extends the root and re-declares
  the third-party bans **plus** the three foundry-internal bans
  (`foundry.providers`, `foundry.runtime`, `foundry.config`). Re-declaration
  is required because ruff REPLACES (does not merge) nested
  `banned-api` tables.

Verified live during the session: imports of `anthropic` and
`foundry.runtime` from a temp file under `src/foundry/core/` both fire
`TID251`; the same `anthropic` import from `src/foundry/api/` fires too;
the legitimate `import anthropic` inside `src/foundry/providers/anthropic.py`
does NOT fire (per-file-ignore active).

The Phase 1 review should consider whether to ALSO commit a CI-level
contract test that asserts the boundary lint catches a deliberately
introduced violation — the live verification this session ran was manual.

## Deviations from `docs/03-development-phases.md` § Phase 0

- **Exit-gate item 5 says "All 18 src/foundry/ subdirectories".** The
  Deliverables list in the prompt enumerates 19 subpackages
  (core, providers, config, catalog, auth, connections, cache, retrieval,
  memory, orchestration, runtime, eval, versioning, configurator, api,
  observability, storage, cli, security). I shipped all 19 and treat the
  "18" in the exit-gate text as an off-by-one in the doc. Not amending
  `docs/03` from this session — flag for the Phase 0 review to confirm.
- **`docs/03` Phase 0 says** "Dependency versions and rationale committed
  to `docs/10-core-framework.md` (or placeholder pointing forward to
  Tier 1 writing)". The full rationale lives in `pyproject.toml`'s leading
  comment and in this handoff note. `docs/10` already has its own
  tier-1-scope content. I have NOT added a duplicate rationale section
  there. Suggested for the review session: add a small forward-pointer in
  `docs/10` or treat this handoff note as authoritative.
- **Removed leftover `main.py`** at repo root, which `uv init` had created
  with a placeholder `print("Hello from agent-foundry!")`. The Phase 0
  spec did not call it out but it was inconsistent with
  `python -m foundry` being the only CLI entry point.

## Files created or modified

### Modified

- `pyproject.toml` — full rewrite from the `uv init` template; pins, scripts,
  pytest, mypy.
- `.gitignore` — unchanged (already had `.venv/`, `__pycache__/`,
  `personal_docs/`).

### Added (top-level)

- `ruff.toml`
- `uv.lock`
- `catalog/README.md`, `catalog/.gitkeep`
- `projects/README.md`, `projects/.gitkeep`
- `docs/_phase_handoffs/phase_0.md` (this file)

### Added (src/foundry/, 114 files including this dir's `__init__.py` files)

- `src/foundry/__init__.py`, `src/foundry/__main__.py`, `src/foundry/py.typed`
- `src/foundry/core/{__init__,agent,tool,session,state,errors,types}.py`
  + `src/foundry/core/ruff.toml`
- `src/foundry/providers/{__init__,anthropic,openai,bedrock,azure,vertex,_registry}.py`
- `src/foundry/config/{__init__,loader,schemas,composition,refs,secrets}.py`
- `src/foundry/catalog/{__init__,loader,promote,schemas}.py`
- `src/foundry/auth/{__init__,token_cache,redactor}.py`
  + `src/foundry/auth/schemes/__init__.py`
- `src/foundry/connections/{__init__,pool,registry,health,descriptors}.py`
- `src/foundry/cache/{__init__,semantic,tool_result,keys}.py`
- `src/foundry/retrieval/{__init__,dense,sparse,hybrid}.py`
  + `src/foundry/retrieval/rerankers/__init__.py`
- `src/foundry/memory/{__init__,coordinator,prompt_assembly}.py`
  + `src/foundry/memory/layers/{__init__,working,episodic,semantic}.py`
- `src/foundry/orchestration/{__init__,compiler,patterns,state_scope,hitl}.py`
- `src/foundry/runtime/{__init__,langgraph_adapter,_langgraph_types,checkpointers}.py`
- `src/foundry/eval/{__init__,harness,schemas,compare,reporter}.py`
  + `src/foundry/eval/scorers/{__init__,exact,llm_judge,rubric}.py`
- `src/foundry/versioning/{__init__,git_backend,artifacts,pins,rollback,audit,refs}.py`
- `src/foundry/configurator/{__init__,meta_agent,session}.py`
  + `src/foundry/configurator/prompts/__init__.py`
  + `src/foundry/configurator/tools/{__init__,fs,registry,build,eval,git,rollback,connections}.py`
- `src/foundry/api/{__init__,app,routes,streaming,auth}.py`
- `src/foundry/observability/{__init__,tracing,logging,artifacts}.py`
- `src/foundry/storage/{__init__,paths,artifacts_store}.py`
- `src/foundry/cli/{__init__,__main__,forge,project,catalog,run,serve,eval,rollback}.py`
  + `src/foundry/cli/tui/__init__.py`
- `src/foundry/security/{__init__,sandbox,injection,validators}.py`

### Added (tests/)

- `tests/unit/test_smoke.py` (20 parametrized assertions)
- `tests/unit/.gitkeep`, `tests/integration/.gitkeep`, `tests/contract/.gitkeep`

### Removed

- `main.py` (leftover from `uv init`).

## Exit-gate confirmation

| Gate | Check | Outcome |
|---|---|---|
| 1 | `uv sync` succeeds on a clean clone (simulated: deleted `.venv` AND `uv.lock`, re-ran) | ✅ pass |
| 2 | `python -m foundry --help` exits 0 and prints help | ✅ pass |
| 3 | `ruff check src/` passes (zero violations) | ✅ pass |
| 4 | `pytest tests/` runs and exits 0 (smoke test asserts `import foundry` + every subpackage) | ✅ pass (20 / 20 cases) |
| 5 | All 18 (actually 19) `src/foundry/` subdirectories exist with `__init__.py` + module-docstring placeholders | ✅ pass — see Deviations §1 |
| 6 | `catalog/` and `projects/` exist with `.gitkeep` + README | ✅ pass |

Manual lint-fires-on-violation check (not in the gate; performed live):

- Adding `import anthropic` + `import foundry.runtime` inside
  `src/foundry/core/` produced two `TID251` violations and removed cleanly.
- Adding `import anthropic` inside `src/foundry/api/` produced `TID251` and
  removed cleanly.
- Adding `import anthropic` inside `src/foundry/providers/anthropic.py`
  produced only `F401` (unused) — `TID251` correctly suppressed.

## Context the next phase (Phase 1) will need

- Pydantic pin is `>=2.13,<3` — Phase 1 schemas should use `model_config`
  v2 idioms (no v1 shims).
- Typer is the CLI framework; `foundry.cli.__main__` is the registration
  point. Phase 1 should replace the `run` command stub with a real handler.
- The Anthropic and OpenAI provider modules are the **only** importers of
  their SDKs. Lint enforces this. If a Phase 1 file needs LangGraph-like
  functionality, it must go through the runtime adapter.
- The smoke test imports every subpackage. If Phase 1 (or any later phase)
  introduces an `__init__.py` that does meaningful work at import time
  (e.g., side effects, side-effecting registrations), confirm it still
  satisfies this contract — or downgrade `test_submodules_import` to a
  static AST check.
- The Phase 0 review session should decide whether the boundary-lint
  verification (currently manual) should land as a CI contract test.

## Open questions logged for review

- **18 vs 19 subpackages** in the exit-gate count (see Deviations §1).
- **Dependency rationale doc placement** — leave in `pyproject.toml` +
  handoff, or also surface in `docs/10`? (Deviations §2.)
