# 91 — v1.1+ backlog

Deferred-from-v1 items, consolidated from the per-phase handoffs and reviews so they aren't lost. Each was an explicit, documented deferral — nothing here is a silent gap. The per-phase handoff in `_phase_handoffs/` holds the context and rationale for each.

## Meta-agent / forge

- Interactive / discuss / resume forge modes (v1 is autonomous-only; docs/62).
- Mid-iteration HITL pause inside a forge run.
- Multi-agent system forging (v1 meta-agent designs single-agent systems).
- Connection + function-node scaffold meta-tools (`build_connection`, `build_function_node`).
- ~~Schema-only eval generation~~ — **DELIVERED WITH GUARDRAILS (2026-07): the studio eval assistant** (`docs/72-web-studio.md` § Eval assistant): a two-step LLM flow (clarifying questions → complete `EvalSpec` draft via the provider abstraction, NOT the meta-agent) with a hard human-review gate — the draft never touches disk until the operator explicitly saves through the validated config-write route. Deterministic scorers only in generated drafts (llm_judge requires human opt-in).
- Drift daemon (scheduled re-eval + alerting).
- ~~Forge web UI~~ — **DELIVERED by Phase 10 (Foundry Studio, v1.1)**: full webapp over every CLI feature incl. the forge console with live trajectory (10c complete 2026-07-16; v1.1.0 tags after the manual browser pass in `docs/_manual_tests/phase_10.md`). Spec: `72-web-studio.md`. Browser E2E automation (Playwright) stays excluded → **v1.2 candidate** below.
- Cross-iteration learning / failure-pattern memory (docs/41).
- Handler-scaffold forbidden-import lint (docs/20 deferral).

## Runtime / orchestration

- Native provider token streaming (`llm.delta` is currently synthesized per content block; SSE assembly per provider per docs/11).
- Postgres checkpointer (multi-host `--workers N`; sqlite is single-host today).
- `rule` / `hybrid` supervisor handoff modes; `force_return_to_supervisor: true`; worker→worker edges.
- Approval timeouts (`timeout_s` / `on_timeout` accepted but unenforced); agent/flow-level approval raise sites.
- Nested `graph` flows; `collect_all` parallel failure mode; multi-terminal output unions.
- Cross-session conversation API (the `turns` read-scope convention still drives multi-turn).
- Cross-session `MemoryStore` (episodic ingests are process-local; docs/26).
- Semantic-cache + memory interaction (currently a documented bypass with a compile warning).
- `SemanticCache.lookup` protocol touch-up: return `(hit, top_similarity)` instead of the backend `last_top_similarity` attribute.

## Providers / integrations

- Bedrock (SigV4/boto3) provider + embedder beyond registered stubs; Azure/Vertex adapters. (The docstring-only placeholder modules `providers/{azure,bedrock,vertex}.py` were deleted post-v1.1 — the studio's stub provider cards reference names, not files; the adapters themselves remain backlog items.)
- `jwt_bearer` RS256 (needs `cryptography` pin); HS256-only today.
- Live-service validation for Redis/pgvector cache + retrieval backends (fake-tested shapes in CI; manual checklists cover live).
- Judge calibration sets + configurable judge output schema (docs/40).
- Per-case `seed` propagation to providers.
- Provider retry loop: adaptive rate-limiter tightening.

## API / serving

- Batch 202+poll mode, dead-letter store, `retry_failed`.
- Mid-graph `inject_input` (v1 semantics: next-run injection).
- Multi-project serving from one process; run-ownership ACLs beyond project-match.
- Circuit breakers.

## Versioning / storage / obs

- Audit-log compaction/archival; tamper-evidence beyond the git commit chain.
- Catalog git tags on promotion.
- Project-scoped retention pins honored by gc (loud warning today).
- `foundry obs trace <run_id>` tree view + raw-SQL surface.
- Checkpoint pruning for completed threads.
- Review TUI: textual front-end (rich-based today; `ReviewModel` is the seam).

## v1.2 candidates (deferred FROM v1.1 planning)

- Playwright browser E2E for Foundry Studio (Phase 10 shipped the manual browser checklist `docs/_manual_tests/phase_10.md` instead; `72-web-studio.md` § Testing strategy).
- Route-level code-splitting of the studio bundle (single 1.98 MB chunk as of 10c; cosmetic for a localhost tool).
- Fix the 10a test-isolation bug: `test_placeholder_page_serves_when_no_frontend_built` fails whenever the sibling `../agent-foundry-studio/dist` exists (10c handoff § known issues).

Canonical copy also lives in operator memory (`project_v1_1_backlog.md`); update both when planning v1.1.
