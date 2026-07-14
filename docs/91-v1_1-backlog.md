# 91 — v1.1+ backlog

Deferred-from-v1 items, consolidated from the per-phase handoffs and reviews so they aren't lost. Each was an explicit, documented deferral — nothing here is a silent gap. The per-phase handoff in `_phase_handoffs/` holds the context and rationale for each.

## Meta-agent / forge

- Interactive / discuss / resume forge modes (v1 is autonomous-only; docs/62).
- Mid-iteration HITL pause inside a forge run.
- Multi-agent system forging (v1 meta-agent designs single-agent systems).
- Connection + function-node scaffold meta-tools (`build_connection`, `build_function_node`).
- Schema-only eval generation.
- Drift daemon (scheduled re-eval + alerting).
- ~~Forge web UI~~ — **being delivered by Phase 10 (Foundry Studio)**, expanded from a forge-only UI to a full webapp over every CLI feature; spec in `72-web-studio.md`, build plan in `03-development-phases.md` § Phases 10a–10c. Browser E2E automation (Playwright) is deliberately excluded from Phase 10 → tracked as a **v1.2 candidate** below.
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

- Bedrock (SigV4/boto3) provider + embedder beyond registered stubs; Azure/Vertex adapters.
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

- Playwright browser E2E for Foundry Studio (Phase 10 ships a manual browser checklist instead; `72-web-studio.md` § Testing strategy).

Canonical copy also lives in operator memory (`project_v1_1_backlog.md`); update both when planning v1.1.
