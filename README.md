# agent-foundry

A personal developer kit for building, evaluating, versioning, and orchestrating multi-agent LLM systems. Configs are text (YAML + markdown + Pydantic), a meta-agent edits them, and the runtime executes compiled specs against any provider.

**Status: v1.1.** All ten core build phases plus Foundry Studio (Phase 10 — a full React web console in the companion [agent-foundry-studio](https://github.com/samueleldridge/agent-foundry-studio) repo) are implemented and independently reviewed. Design docs (normative, 35 specs) live in [docs/](docs/README.md). This is a reference release — not currently accepting external contributions.

**Highlights**

- **A meta-agent that builds agents** — `foundry forge` discovers catalog tools, scaffolds new ones, writes agent configs, evals every iteration, commits each one to git, and rolls back regressions — sandboxed, cost-capped, and rate-limit resilient.
- **Everything is text, everything is versioned** — agents are YAML + markdown + Pydantic; tools, prompts, and connections version independently with one-command audited rollback.
- **Eval-driven by construction** — a deterministic eval harness (tool/agent/project scopes, LLM-judge scorers, cross-version comparison) is the meta-agent's objective function and your CI gate.
- **Production surfaces included** — FastAPI serving with SSE/WebSockets and human-in-the-loop approvals, OpenTelemetry + local cost analytics, and a full web console with a live multi-agent flow graph.
- **1100+ backend tests, zero keys needed** — the entire suite runs offline against mock transports; `mypy --strict` and import-boundary linting enforced throughout.

## Quickstart

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). One provider API key is enough to run everything — the examples and the meta-agent default to OpenAI (`openai/gpt-5-mini`), and swapping to Anthropic is a one-line YAML change.

```bash
uv sync                          # install pinned deps
uv run foundry doctor            # environment + config health check
```

Provide your API key however you like — the CLI reads it from the process
environment. For local work the easiest path is a `.env` in the repo root
(gitignored), which `foundry` auto-loads:

```bash
cat > .env <<'EOF'
OPENAI_API_KEY=sk-...             # or ANTHROPIC_API_KEY (see provider swap below)
HELLO_SERVICE_API_KEY=dummy       # hello's demo tool connection; any value works
EOF
uv run foundry doctor            # the `env_file` line confirms what was loaded
```

A real exported environment variable always wins over `.env`; opt out entirely
with `FOUNDRY_NO_ENV_FILE=1`, or point elsewhere with `FOUNDRY_ENV_FILE=path`.
Prefer not to use a file? Just `export OPENAI_API_KEY=...` in your shell.

### Run the hello project

```bash
uv run foundry run hello --input '{"name": "Ada"}'   # bare names resolve against ./projects/
```

(`hello` calls one demo tool over an authenticated connection, so it needs
`HELLO_SERVICE_API_KEY` — already in the `.env` above.)

This compiles `projects/hello/system.yaml` → LangGraph, calls the model (plus one catalog tool through a pooled, authenticated connection), and writes a full run artifact (events, LLM calls, tool calls, final state) under `~/.foundry/runs/<run_id>/`.

Provider swap is a one-line YAML change — edit `projects/hello/agents/hello_agent/agent.yaml` → `model_binding.provider: anthropic` + a model name (e.g. `claude-haiku-4-5`), set `ANTHROPIC_API_KEY`, re-run the same command.

Other example projects: `projects/rag_hello` (semantic cache + hybrid retrieval + a fully local rerank stage — runs on the same single `OPENAI_API_KEY`), `projects/memory_hello` (three memory layers + function nodes), `projects/team_hello` (supervisor + workers + human-in-the-loop approval).

### Serve a project as an API

```bash
uv run foundry serve hello --port 8000
# then:
curl -s -X POST localhost:8000/run -H 'content-type: application/json' -d '{"name": "Ada"}'
```

`POST /stream` gives SSE with `Last-Event-ID` resume; `WS /ws` is bidirectional (inject input, approve HITL pauses, cancel); `POST /batch` fans out a list of inputs under one cost budget. OpenAPI at `/openapi.json` is generated from the project's spec.

### Foundry Studio — the web console

```bash
uv run foundry studio
```

Every CLI feature behind a polished React UI (dark/light): project browsing, config editing with live server-side validation and commit-on-save, catalog explorer, chat with streamed responses and in-chat human-approval gates, a **multi-agent flow-graph visualisation**, a **live forge console** (launch the meta-agent, watch scores/commits/backoffs stream), AI-assisted eval authoring with a human review gate, per-provider API-key management with a model browser, cost/latency dashboards, and user-composable widget dashboards. The frontend lives in the companion [agent-foundry-studio](https://github.com/samueleldridge/agent-foundry-studio) repo (checked out as a sibling directory, `npm run build`); `foundry studio` serves the built app and its control-plane API from one port — and serves an API-only placeholder if the frontend isn't built.

### Evaluate

```bash
uv run foundry eval hello evals/greeting.yaml          # project eval, per-case scores
uv run foundry eval hello evals/greeting.yaml --fail-under 0.9   # CI gate
uv run foundry eval compare --tool word_count v1 v2    # side-by-side version comparison
```

### Forge a new system with the meta-agent

```bash
uv run foundry project new my_agent
uv run foundry forge my_agent --description "..." --eval path/to/eval.yaml \
  --threshold 0.9 --max-iter 5 --max-cost-usd 5
```

The meta-agent discovers catalog tools, scaffolds project-local ones, writes agent configs, evals each iteration, commits per iteration on `foundry/my_agent`, and rolls back regressions. It is sandboxed to the project directory.

### Versioning, rollback, observability

```bash
uv run foundry versions hello                 # commits + per-artifact versions
uv run foundry rollback hello --prompt hello_agent --to v1   # audited one-pin rollback
uv run foundry review hello                   # review TUI: commits, eval scores, rollback
uv run foundry obs cost --project hello --since 7d           # cost breakdown
```

### Five-minute end-to-end demo (no API key needed)

```bash
uv run python scripts/demo_phase9.py   # eval → serve → regression → rollback → cost, mock provider
```

## Verifying a fresh setup

`uv run pytest tests/` (1100+ tests, no network or keys needed), then the live-key smoke checklists in [docs/_manual_tests/](docs/_manual_tests/) — start with `phase_1.md` (basic runs) and `phase_9.md` (docker + OTel + live forge demo).

## Layout

- `src/foundry/` — the framework (core, providers, config, orchestration, runtime, eval, versioning, configurator, api, observability, storage, security, cli).
- `catalog/` — shared, versioned tools/connections/retrievers. Promotion is human-gated (`foundry catalog promote`).
- `projects/` — configured systems (the four examples above; yours go here).
- `docs/` — the 35 normative specs, phase handoffs, retros, demos, manual test checklists.
- `deploy/` — Dockerfile + per-platform manifests.
- `scripts/` — demos + release helpers; `scripts/bundle_studio_assets.sh` builds the sibling frontend repo and copies its `dist/` into the wheel's packaged studio assets.

## Docs

Start at [docs/README.md](docs/README.md). Architecture: [docs/01-architecture-overview.md](docs/01-architecture-overview.md). Dev UX and the full CLI map: [docs/82-dev-ux.md](docs/82-dev-ux.md). v1.1+ backlog: [docs/91-v1_1-backlog.md](docs/91-v1_1-backlog.md).
