# Phase 9 handoff — observability + dev UX + security + deploy (FINAL)

**Session date:** 2026-07-13
**Branch:** `main`; tagged `v1.0.0` (not pushed)
**Status:** Phase 9 implementation complete; awaiting AI review + the
operator manual smoke test (docs/_manual_tests/phase_9.md). The dev
sandbox had no API keys (LLM via httpx.MockTransport), no Docker daemon
(`docker info` fails — build/run is manual §2–3), no live OTel collector,
no Redis/Postgres. Everything below was verified against the real
framework with only the provider HTTP layer substituted.

## Pre-work (Phase 8 re-review findings) — landed first

All five were committed at the top of this phase (see `git log
320c1aa..102622d`): binary-WS-frame structured error; artifact-route
project-ownership checks under shared FOUNDRY_HOME; rate-limit permit
re-acquisition on retried provider attempts; per-item `can_accept()` in
batch; request-size guard (structured 413) + batch-size cap.

## What this session built

1. **`foundry.observability`** (docs/80) —
   - `events.py`: an `ObservabilityDispatcher` hooked into the runtime
     `EventEmitter` (every emitted RunEvent, CLI/API/eval alike) with
     per-handler degradation guards (`observability.degraded` logged once;
     a broken exporter can never fail a run). Three handlers: span
     mirror, metrics, SQLite mirror.
   - **Span mirror**: subsystems that emit events but held no span of
     their own get retroactive spans — `foundry.tool`, `handoff`,
     `state_transition`, `function_node`, `connection`, `embed`,
     `cache.semantic`, `cache.tool`, `retrieval`, `rerank`, `memory`,
     `approval` — start times back-computed from `latency_ms`, parented
     to whatever span is current at emit (`foundry.node`/`foundry.llm`).
     `foundry.run/node/llm/eval/rollback` keep their native spans.
   - `metrics.py`: the docs/80 instrument catalogue (run totals/duration/
     cost, run token counters keyed project+provider+model, llm calls/
     latency/cost, tool calls/latency, handoffs, eval runs + score gauge,
     semantic-cache saved cost, embed cost, rollback + deployment
     counters). A per-run tracker supplies project/provider/model dims
     that terminal + llm.completed events don't carry.
   - `store.py`: `~/.foundry/observability.db` (FOUNDRY_HOME-relative)
     WAL SQLite mirror — tables `runs`, `llm_calls`, `tool_calls`,
     `handoffs`, `evals` + `schema_meta(schema_version=1)`; query surface
     (cost breakdown by model/day/agent, tool failures, latency
     percentiles, recent runs, eval rows). Kill switch:
     `FOUNDRY_OBS_MIRROR=off`.
   - `redaction.py`: default-deny key denylist + secret-shaped value drop
     + 500-char preview truncation. NOTE: the shared denylist regex
     matches `token`, which would have eaten every token-COUNT field;
     counts are exempted by stripping the plural `tokens` before matching.
   - `exporters/`: console; OTLP grpc/http (standard OTel env vars);
     LangSmith + Langfuse as **OTLP-ingest adapters** (no extra SDKs —
     both vendors accept OTLP with auth headers; missing env → structured
     ConfigError). `tracing.configure_observability()` reads
     `FOUNDRY_TRACING` (off default) and installs the SDK once per
     process; called from the CLI app callback + `create_app`.
   - Run-artifact completeness (docs/81): `inputs.json` (gated on
     `capture_inputs`, written by CLI + API run starts), `outputs.json`
     (from `run.completed.final_output`), `state_transitions.jsonl`.
   - Eval results mirror into the store + metrics from
     `write_eval_artifact`; rollbacks record `foundry.rollback.total`.
2. **`foundry.cli.obs`** — `foundry obs cost|tool-failures|p95|runs|
   eval-trend`, tabular + `--json`, `--since 7d/24h/30m`; reads the
   SQLite mirror (docs/80 invariant 6). Integration test cross-checks
   CLI == store == events.jsonl sums == span stream on one run.
3. **`foundry.storage`** (docs/81) — `StorageBackend` protocol;
   `FilesystemBackend` real; S3/S3-compatible/Azure/GCS as lazy-import
   translations that fail closed with install hints (SDKs not pinned);
   `select_backend()` env factory. Retention: global + per-project pins,
   `gc` (pinned exempt; `--force` logged loudly + reported), monthly
   tar.gz archival, `parse_duration`. `copy_between` backend migration.
   CLI: `foundry storage stats|gc|archive|pin|unpin|list-pinned`.
4. **`foundry.security`** (docs/83) —
   - `sandbox.PathSandbox`: canonicalise-then-check read allowlist +
     single write root + denied subtrees (`evals/`, `.foundry/`). The
     configurator meta-tool guards now DELEGATE path logic here (forge
     side effects — violation record + cancel-token — stay put; all 40
     existing sandbox tests unchanged). 50-case malicious-path write fuzz
     + read fuzz, all refused.
   - `injection.py`: every runtime-interpolated tool result is wrapped in
     `<tool_result tool="…" version="…" untrusted="true">` with embedded
     closing tags entity-escaped (no boundary breakout); every
     tool-bearing agent's system prompt gets `TOOL_RESULT_BOUNDARY_NOTE`
     (the prompt references the boundary explicitly — docs/03 exit gate).
     `unwrap_tool_output` exists for test fakes that stand in for the LLM.
   - `validators.py`: `ensure_no_secret_leak` (reports pattern names,
     never values) + `validated_json`.
   - Credential-leak contract test: end-to-end run with known fake keys →
     zero hits across exported spans, every artifact file, and the mirror.
5. **`foundry.testing`** (docs/82) — `RunContextFixture`,
   `MockConnection(+Accessor)`, `MockProvider` (scripted + call
   recording), `scripted_transport` (anthropic/openai MockTransport
   shapes — compiled projects run against it end-to-end), `MockEmbedder`,
   `MockRetriever`, `MockReranker(+Accessor)`, `MockSecretsResolver`;
   `make_state` / `StateBuilder` / `assert_state_transition` over the
   compiled reducers; `pytest_plugin` auto-loaded by **`foundry test`**
   (exit codes 0/1/2, 3 for `--with-eval` under threshold).
6. **`foundry doctor`** — the docs/82 check catalogue (versions, roots,
   per-project config validation, env-var validation for checkpointer/
   rate-limiter/tracing/storage, FOUNDRY_HOME writability, sandbox
   import, git branch state); exit 0/1(`--strict` warns)/2; `--json`.
7. **`foundry review`** (docs/52 § Review TUI) — built on **rich, not
   textual** (decision recorded in pyproject + the module docstring):
   `ReviewModel` programmatic layer (commits with audit-typed kinds +
   eval deltas, per-commit diff + eval context + operator, per-artifact
   version/pin rows with eval scores, eval trajectory, approvals,
   connections, `rollback_to`) + a rendered screen with 4 tabs + an
   `input()`-driven loop; rollback requires typing the short sha. Tests
   drive the programmatic layer including a real rollback commit.
8. **`foundry.deploy` + `foundry deploy`/`compute-version`** (docs/84) —
   deterministic content-hash system version; pre-deploy eval gate
   (refusal → exit 1, recorded); platform helpers kubectl/ecs/cloud-run/
   fly/nomad (argv translations; subprocess only outside `--dry-run`;
   missing binary → exit 2) + noop; every invocation appends ONE
   `non_commit` audit entry (completed/failed/refused); docs/84 exit
   codes 0–4. Optional `projects/<p>/deploy/<target>.yaml` defaults.
9. **Container packaging** — repo-root `Dockerfile` (two-stage uv build,
   non-root, python-urllib healthcheck — slim has no curl, provenance
   LABELs); `deploy/` manifests for k8s / ECS Fargate / Cloud Run /
   Azure Container Apps / Fly / Nomad (all syntax-validated in tests),
   `env.template` (every honoured FOUNDRY_* var + the secrets-provider
   plug-point note), `docker-compose.otel.yaml` + collector config for
   manual §3, `deploy/README.md`.
10. **Catalog dev-UX polish** — `foundry catalog list|show` implemented
    over the multi-root `catalog_entries` (shadow-aware).
11. **Contract tests** (docs/80) — every RunEvent's field set frozen in
    `tests/contract/test_observability_schema.py` (drift fails CI;
    renames/removals are major-version events); every mirrored span
    checked against the docs/80 mandatory-attribute table.
12. **Demo** — `scripts/demo_phase9.py`: forge-stand-in bootstrap → eval
    1.00 → real FastAPI POST /run → marker-gated prompt regression caught
    by the eval (0.00) → `foundry rollback --prompt` (audited commit) →
    eval recovers (1.00) → `foundry obs cost` breakdown. 0.7s wall time.

## Dependency decisions

- **rich >=15** added as an explicit pin (was already transitive via
  typer) — `foundry.cli.tui` imports it directly. **textual rejected**
  for v1: a full TUI framework pin for one minimal review page isn't
  warranted; the ReviewModel/render split means a textual front-end can
  be added in v1.1 without touching the data layer.
- **No boto3 / azure-storage-blob / google-cloud-storage / langsmith /
  langfuse SDKs pinned** — cloud storage backends lazy-import and fail
  closed with install hints (same policy as redis); the LangSmith/
  Langfuse exporters need no SDK at all (OTLP ingest).

## Deviations from the docs (deliberate)

1. **Metric dimension notes** (docs/80 table): `foundry.run.input_tokens`
   etc. are recorded per `llm.completed` with project+provider+model dims
   (the run-level event carries no provider/model); `foundry.run.cost_usd`
   dims are project+worker_id with the per-model mix on
   `foundry.llm.cost_usd`. Documented in `metrics.py`.
2. **SQLite mirror tables** are the five the Phase 9 deliverable names
   (runs/llm_calls/tool_calls/handoffs/evals). `forges`/`rollbacks`
   mirror rows (docs/80 sketch) are NOT separate tables in v1 — forge
   events flow to spans/metrics and the audit log remains the regulatory
   record; add tables additively if `foundry obs forge` queries land in
   v1.1.
3. **`foundry obs` subcommand subset**: cost / tool-failures / p95 /
   runs / eval-trend (the Phase 9 deliverable list + exit gate). The full
   docs/80 catalogue (trace-tree rendering, audit queries via obs, raw
   SQL) is v1.1 surface; `foundry versions`/`review` cover audit
   browsing locally.
4. **Storage retention** implements kind="runs" concretely (evals + forge
   artifacts live under `~/.foundry/runs/<id>` in this codebase, so one
   tree covers the deliverable); per-kind policy knobs exist on the
   model. Multi-host S3 round-trip + clock-advanced retention are manual
   checklist items (no cloud SDKs in the sandbox).
5. **Doctor checks that would need live services** (Postgres/Redis/OTel
   reachability) validate configuration shape and warn instead of
   connecting — doctor stays fast and dependency-free.
6. **The demo's forge step is a bootstrap stand-in** (copy + branch +
   commit): a live forge needs a real meta-agent key. The live variant is
   manual §4. The regression/rollback/eval/cost steps are fully real.
7. **`foundry.observability.cli` / `foundry.storage.cli` module homes**:
   obs CLI lives at `foundry/cli/obs.py` (matching every other Phase ≤8
   CLI); storage executors live at `foundry/storage/cli.py` per docs/81's
   module layout. Cosmetic split, documented here.
8. **Span mirror is retroactive** (spans created at event time with
   back-computed start): tool/connection/cache spans are children of the
   node/llm span current at emit, not wrapping their exact call frames.
   Attribute-complete and correctly parented; converting to inline spans
   is invasive for zero attribute gain (core cannot import observability).

## Exit-gate confirmation

| Gate | How verified | Outcome |
|---|---|---|
| Traces with all mandatory attributes; drift caught by contract test | `test_observability_schema.py` (field-set freeze + span attribute table); `test_observability_exit_gate.py` (real run: native + mirrored spans, run_id on every span) | ✅ |
| Metrics aggregate cleanly (7-day project cost) | `test_seven_day_project_cost_is_computable_from_the_metric_stream` + `test_metric_stream_isolates_project_cost` (InMemoryMetricReader) | ✅ |
| SQLite mirror captures same events; obs matches OTel stream | exit-gate test cross-checks store totals == events.jsonl sums == obs `--json` output | ✅ |
| `foundry obs cost --project hello --since 1d` usable breakdown | demo script step 5 (recorded in docs/_demos/phase_9.md) + integration test | ✅ |
| RunArtifact completeness | metadata/inputs/outputs/state_transitions/llm_calls/tool_calls asserted on a real run | ✅ |
| Review TUI: commits, versions+scores, diffs, rollback | `test_tui_review.py` drives ReviewModel incl. a real rollback commit; screen render asserted | ✅ (programmatic) / ⏳ operator (interactive feel) |
| Injection boundary + prompt reference; documented in 83 | `test_injected_tool_output_arrives_inside_typed_boundary` (real dispatch path); docs/83 already specifies the design this implements | ✅ |
| Credential-leak contract: zero hits | `test_credential_leak_contract_zero_hits_across_all_surfaces` (spans + artifacts + mirror) | ✅ |
| Sandbox 50-case malicious-path fuzz all refused | `test_security_sandbox_fuzz.py` (50 write shapes + read fuzz) | ✅ |
| docker build + run serves end-to-end with OTel export | Dockerfile/manifests/compose syntax-validated in `test_deploy_manifests.py`; **daemon unavailable in sandbox** → manual §2–3 | ⏳ operator |
| ≤5-minute top-to-bottom demo | `scripts/demo_phase9.py` — 0.7s, mock provider; live variant manual §4 | ✅ (mock) / ⏳ operator |

## Definition-of-done confirmation

- `uv run ruff check src/ tests/` — zero violations.
- `uv run mypy --strict src/foundry/` — passes (210 files).
- `uv run pytest tests/` — **983 passed, 1 skipped** (Phase 8 post-review
  baseline 791+1, +15 pre-work, +176 Phase 9).
- langgraph confined to the adapter modules; `foundry.api` imports
  nothing from `foundry.configurator` (boundary lint + contract test).
- `run_id` threaded through logs, spans (native + mirrored), metrics
  correlation, artifacts, and the mirror.
- No secrets in code/configs/fixtures; the credential-leak contract test
  enforces it continuously.
- Conventional commits, no co-author lines; `v1.0.0` tagged (not pushed).

**v1 is COMPLETE pending the Phase 9 review session + the operator
manual smoke test. There is no Phase 10 — next steps live in
docs/90 § "When you're done with v1" and the v1.1 backlog memory.**
