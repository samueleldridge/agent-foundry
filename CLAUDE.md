# CLAUDE.md — agent-foundry

**agent-foundry** is a personal developer kit for building, evaluating, versioning, and orchestrating multi-agent LLM systems. The core claim: configs are text (YAML + markdown + Pydantic), a meta-agent edits them, and the runtime executes compiled specs against any provider.

**Status: v1 COMPLETE (tagged `v1.0.0`, 2026-07-13).** All ten phases (0–9) are implemented and reviewed; `src/foundry/` ships the full documented surface. `docs/` remains the source of truth (33 specs across 9 tiers) — where the implementation deliberately deviates, the phase handoff in `docs/_phase_handoffs/` records it. The build was sequenced per [docs/03-development-phases.md](docs/03-development-phases.md) and [docs/90-implementation-plan.md](docs/90-implementation-plan.md); maintenance work should still honour the per-phase exit gates and review pattern there. Operator quickstart: root [README.md](README.md). v1.1+ backlog: [docs/91-v1_1-backlog.md](docs/91-v1_1-backlog.md).

## Read these first (in order)

1. [docs/00-vision-and-scope.md](docs/00-vision-and-scope.md) — what this is, non-goals, guiding principles.
2. [docs/01-architecture-overview.md](docs/01-architecture-overview.md) — layers, primitives, directory layout, dependency rules.
3. [docs/03-development-phases.md](docs/03-development-phases.md) — the phase you're on + its exit gate.
4. [docs/90-implementation-plan.md](docs/90-implementation-plan.md) — paste-ready prompts + fresh-session-per-phase review pattern.

The rest of `docs/` is tier-organised. Jump to the tier matching the phase you're implementing — full map in [docs/README.md](docs/README.md).

## Invariants

These hold across every session. Violations should be reverted, not papered over.

### Code & architecture

- **Python 3.12**, package-managed by **`uv`** (not pip / poetry / pipenv). `uv.lock` is committed.
- **Pydantic v2** for every config schema. YAML loads → Pydantic; no untyped dicts at boundaries.
- **Provider-agnostic** is a hard requirement. Every provider-specific code path goes through `foundry.providers` adapters. From Phase 1 onward, the trivial example must run against ≥2 providers with only a `model_binding.provider` change in YAML.
- **Per-artifact versioning.** Tools, prompts, connections, retrievers each version independently. Pins live in `system.yaml` (tools, connections, retrievers) and `agent.yaml` (prompts).
- **Configs are text.** Never introduce a binary or DB-only config path. Markdown + YAML + Python schemas only.
- **State visibility is structural.** A node's allowed reads/writes are compile-time enforced (TypedDict projection per agent). Do not add runtime-only checks as a substitute.
- **Observability is the audit trail.** Every execution path threads a `run_id` through logs, OTel spans, and the `RunArtifact`. Never log secrets.

### Import boundaries (enforced by `ruff.toml`)

- `foundry.core` imports nothing foundry-internal — only stdlib + `pydantic`.
- `langgraph` and `langchain_*` are imported ONLY in `foundry/runtime/langgraph_adapter.py` and `foundry/runtime/_langgraph_types.py`. Lint must fail otherwise.
- `foundry.api` does NOT import `foundry.configurator`. Configurator is dev-time; API is run-time.
- `foundry.configurator` is the sole module that composes `foundry.eval` + `foundry.versioning` as a unit (other consumers may use either alone).

Full dependency diagram: [docs/01-architecture-overview.md § Module ownership and dependency rules](docs/01-architecture-overview.md#module-ownership-and-dependency-rules).

### Three top-level trees

- `src/foundry/` — the framework. The meta-agent's `write_file` tool MUST refuse writes here.
- `catalog/` — shared, versioned artifacts. The meta-agent READS only; promotion is human-gated via `foundry catalog promote`.
- `projects/` — configured systems. The meta-agent writes here, scoped to a single project per session.

### Commits & branches

- **Conventional commits**: `feat:` `fix:` `chore:` `docs:` `refactor:` `test:` `ci:` `build:` `perf:` `style:`. Scope encouraged (`feat(core): …`, `docs(impl): …`).
- **No Claude co-author line.** Never add `Co-Authored-By: Claude` to commits in this repo.
- **Branch model**: framework work on `main`; per-project work on `foundry/<project>` branches once projects exist.
- **No `--no-verify`** to skip hooks. Fix the hook failure instead.
- **Atomic commits per logical chunk** — one feature, one schema, one module at a time. Don't batch cross-domain changes.

### Confidentiality

- **No institution-name leakage** in repo content. No firm names, client names, or internal system names. Use generic placeholders (`example_corp`, `internal_db`, `firm_x`) everywhere.
- **No secrets in YAML or in tests.** Env vars + fixtures only. The secret-literal scan in `foundry.config.secrets` catches credentials at load time — do not bypass it.
- Persona in repo docs is **"AI engineer"** — not "Lead AI engineer" or any other title.

## Phased build workflow

The project ships in 10 phases (0–9). Each phase has:

1. A spec in [docs/03-development-phases.md](docs/03-development-phases.md) — the source of truth for deliverables + exit gate.
2. Paste-ready prompts in [docs/90-implementation-plan.md](docs/90-implementation-plan.md) — implementation + review, **fresh Claude Code session per phase**.
3. A manual smoke-test checklist in `docs/_manual_tests/phase_<N>.md` — the operator runs these by hand after the AI review passes. (Created alongside each phase prompt.)
4. A handoff note at `docs/_phase_handoffs/phase_<N>.md` written at session end.
5. A retro at `docs/_retros/phase_<N>.md` + a smoke-test demo at `docs/_demos/phase_<N>.md`.

**Do not move to phase N+1 until both the AI review and the manual smoke test pass.** This is non-negotiable — the fresh-session pattern only protects against drift if the gates actually hold.

### Definition of done (every phase)

- All exit-gate items from `docs/03-development-phases.md` § Phase <N> pass.
- `uv run ruff check src/ tests/` — zero violations.
- `uv run mypy --strict src/foundry/` — passes.
- `uv run pytest tests/` — passes; new code has unit tests, new slices have integration tests.
- No secrets in code, configs, or test fixtures.
- Every execution path threads a `run_id` through logs, traces, and artifacts.
- Phase handoff note written; commits follow conventional format with no co-author line.

## Hard constraints when implementing

These bind your behavior directly. If you find yourself tempted to violate one, stop and surface the conflict to the operator instead.

- **Stay scoped to the current phase.** If the session prompt scopes you to Phase N, refuse to implement Phase N+1 deliverables even if asked or "it's small". The phase exit gate is the contract; everything else waits for a fresh session.
- **Honor import boundaries.** Never reach across them to "just make it work". Either add a method to the adapter or refactor the boundary properly — and if you can't, surface the conflict.
- **Don't add features not on the phase's deliverable list.** Anything outside v1 belongs in the v1.1+ backlog (operator memory). If you think a deliverable is missing from the spec, ask before implementing.
- **The meta-agent never writes into `src/foundry/` or `catalog/`.** The sandbox refuses both. If your design seems to require it, the design is wrong.
- **Don't switch frameworks mid-phase** even if PydanticAI / Strands / autogen look attractive in the moment. Capture the cost in the retro instead.

## Quick commands

```bash
# Setup (Phase 0 onward)
uv sync                                # install deps from uv.lock
uv run python -m foundry --help

# Lint + types + tests
uv run ruff check src/ tests/
uv run mypy --strict src/foundry/
uv run pytest tests/

# Phased work (each step is a fresh Claude Code session)
# 1. Implementation: paste docs/90 § Phase <N> implementation prompt
# 2. Review (separate session): paste docs/90 § Phase <N> review prompt
# 3. Manual smoke test: follow docs/_manual_tests/phase_<N>.md
```

## Where to find context

- **Architecture decisions** → [docs/01-architecture-overview.md](docs/01-architecture-overview.md).
- **Spec for the layer you're touching** → `docs/<tier><N>-*.md`; map in [docs/README.md](docs/README.md).
- **Phase exit gate** → [docs/03-development-phases.md](docs/03-development-phases.md) § Phase <N>.
- **Past phase handoffs** → `docs/_phase_handoffs/` (read prior ones before starting next).
- **Locked decisions + resolved open questions** → operator memory (auto-loaded into every session).
- **Seed concept (provenance only)** → `personal_docs/meta-agent-configurator.jsx`. Do not ship this content into `docs/` or `src/`.
