# Changelog

## v1.1 — 2026-07-16

Foundry Studio (Phase 10): a locally-served web console putting every CLI
feature behind a UI.

- `foundry studio` — control-plane FastAPI server (`foundry.studio`)
  serving the built React SPA from the companion `agent-foundry-studio`
  repository (sibling checkout or `FOUNDRY_STUDIO_DIST`), with an
  API-only placeholder when the frontend isn't built.
- Control-plane routes for projects, config editing (validate +
  commit-on-save), catalog, doctor, observability queries, runs, evals,
  versions/rollback, connections, deploy, testing, and dashboard
  layouts.
- Live forge console: launch the meta-agent, stream scores, commits, and
  backoffs over SSE; cancel mid-run.
- Chat frontend for Q&A-shaped projects with streamed responses and
  in-chat human-approval gates; multi-agent flow-graph visualisation.
- AI-assisted eval authoring (two-step LLM draft flow) with a hard
  human-review gate; per-provider API-key management with a model
  browser.
- New import boundary: `foundry.api` must not import `foundry.studio`
  (ruff-enforced + contract-tested).

## v1.0.0 — 2026-07-13

First complete release: all ten build phases (0–9) implemented,
reviewed, and gated per docs/03.

- Core framework: Pydantic-v2 config schemas (YAML + markdown prompts),
  compile-time state visibility, per-artifact versioning with git as the
  backbone, audited rollback.
- Provider-agnostic runtime: LangGraph adapter behind a hard import
  boundary; Anthropic + OpenAI adapters, embedders, pricing, rate
  limiting; provider swap is a one-line YAML change.
- Orchestration: single-agent, supervisor/worker teams, function nodes,
  human-in-the-loop approvals; memory layers, semantic cache,
  retrieval/RAG stages.
- Eval harness: project/tool/connection scopes, deterministic + LLM
  judge scorers, eval-driven iteration, `--fail-under` CI gates.
- Meta-agent (`foundry forge`): catalog discovery, project scaffolding,
  iterate-to-threshold loop with cost/iteration/plateau guardrails,
  sandboxed writes, per-iteration commits and rollbacks.
- Serving: `foundry serve` — REST + SSE (`Last-Event-ID` resume) +
  WebSocket + batch under one cost budget, generated OpenAPI.
- Observability as the audit trail: run_id-threaded structured logs,
  OTel spans, run artifacts, local SQLite obs mirror, cost dashboards.
- Full CLI (`run`, `eval`, `forge`, `versions`, `rollback`, `review`,
  `obs`, `doctor`, `catalog`, `project`, `serve`, `test`), review TUI,
  and the deploy/ platform manifests.
