# agent-foundry

A personal developer kit for building, evaluating, versioning, and orchestrating multi-agent LLM systems. Configs are text (YAML + markdown + Pydantic), a meta-agent edits them, and the runtime executes compiled specs against any provider.

**Status: v1.0.0.** All ten build phases are implemented and reviewed. Design docs (normative) live in [docs/](docs/README.md).

## Quickstart

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/). One provider API key is enough to run everything (Anthropic or OpenAI).

```bash
uv sync                          # install pinned deps
export ANTHROPIC_API_KEY=sk-...  # or OPENAI_API_KEY (see provider swap below)
uv run foundry doctor            # environment + config health check
```

### Run the hello project

```bash
export HELLO_SERVICE_API_KEY=dummy   # hello's demo catalog connection; any value works
uv run foundry run hello --input '{"name": "Ada"}'   # bare names resolve against ./projects/
```

This compiles `projects/hello/system.yaml` → LangGraph, calls the model (plus one catalog tool through a pooled, authenticated connection), and writes a full run artifact (events, LLM calls, tool calls, final state) under `~/.foundry/runs/<run_id>/`.

Provider swap is a one-line YAML change — edit `projects/hello/agents/hello_agent/agent.yaml` → `model_binding.provider: openai` + a model name, set `OPENAI_API_KEY`, re-run the same command.

Other example projects: `projects/rag_hello` (semantic cache + hybrid retrieval), `projects/memory_hello` (three memory layers + function nodes), `projects/team_hello` (supervisor + workers + human-in-the-loop approval).

### Serve a project as an API

```bash
uv run foundry serve hello --port 8000
# then:
curl -s -X POST localhost:8000/run -H 'content-type: application/json' -d '{"name": "Ada"}'
```

`POST /stream` gives SSE with `Last-Event-ID` resume; `WS /ws` is bidirectional (inject input, approve HITL pauses, cancel); `POST /batch` fans out a list of inputs under one cost budget. OpenAPI at `/openapi.json` is generated from the project's spec.

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

`uv run pytest tests/` (~1000 tests, no network or keys needed), then the live-key smoke checklists in [docs/_manual_tests/](docs/_manual_tests/) — start with `phase_1.md` (basic runs) and `phase_9.md` (docker + OTel + live forge demo).

## Layout

- `src/foundry/` — the framework (core, providers, config, orchestration, runtime, eval, versioning, configurator, api, observability, storage, security, cli).
- `catalog/` — shared, versioned tools/connections/retrievers. Promotion is human-gated (`foundry catalog promote`).
- `projects/` — configured systems (the four examples above; yours go here).
- `docs/` — the 33 normative specs, phase handoffs, retros, demos, manual test checklists.
- `deploy/` — Dockerfile + per-platform manifests.

## Docs

Start at [docs/README.md](docs/README.md). Architecture: [docs/01-architecture-overview.md](docs/01-architecture-overview.md). Dev UX and the full CLI map: [docs/82-dev-ux.md](docs/82-dev-ux.md). v1.1+ backlog: [docs/91-v1_1-backlog.md](docs/91-v1_1-backlog.md).
