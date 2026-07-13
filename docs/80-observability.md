# 80 — Observability

## Purpose

This doc consolidates the foundry's observability surface: the structured event taxonomy, the three transport layers (OTel traces, OTel metrics, local SQLite mirror), the run-artifact format, derived metrics, the `foundry obs` CLI, and the integration patterns for external backends. Builds on `01-architecture-overview.md` § Observability summary, `10-core-framework.md` § Streaming events, and the per-tier observability events scattered through Tiers 2–6.

The design principle from Tier 0: **observability is built as an audit trail first, monitoring source second, debugging aid third**. The events are structured for aggregation, not just for humans to skim. This is what makes monitoring dashboards a configuration exercise rather than an instrumentation rewrite.

Three load-bearing properties:

1. **Every consequential action emits a typed event.** No silent operations. The audit completeness invariant cuts across the framework — LLM calls, tool calls, connections, handoffs, state transitions, evals, forges, rollbacks all emit.
2. **Attribute shape is frozen per major version.** Downstream dashboards can rely on the schema; additions are additive; renames/removals require a major version bump.
3. **Three transports for three audiences.** OTel traces for debugging, OTel metrics for monitoring, local SQLite mirror for cross-run queries during dev. All driven by the same event stream.

## Module layout

```
src/foundry/observability/
├── __init__.py            public surface
├── tracing.py             OTel tracer setup; span helpers
├── metrics.py             OTel metrics: counters / histograms / gauges
├── events.py              RunEvent → exporter dispatch
├── store.py               local SQLite event-mirror; cross-run query helpers
├── exporters/
│   ├── otlp.py            OTLP exporter (HTTP / gRPC)
│   ├── langsmith.py       opt-in LangSmith adapter
│   ├── langfuse.py        opt-in Langfuse adapter
│   └── console.py         dev-only stdout exporter
├── artifacts.py           per-run artifact writer (see 81-storage-and-artifacts)
├── redaction.py           field-level redaction per ObservabilityConfig
└── cli.py                 foundry obs <subcommand> dispatch
```

## The event taxonomy (consolidated)

Every `RunEvent` subclass from `10-core-framework.md` § Streaming events plus framework-emitted spans. Grouped by phase:

> **Machine-checked contract**: the enforced attribute schema for this taxonomy is extracted to [80-observability-attributes.yaml](80-observability-attributes.yaml). `tests/contract/test_observability_schema.py` parses that file at test time and compares it against the code's actual event field sets and emitted span attributes, so doc ↔ code drift fails CI. The tables below stay the human-readable narrative (they include roadmap-optional attributes, `?`-marked); when the schema changes, update the YAML **and** the matching table row together.

### Run lifecycle

| Event | When | Key attributes |
|---|---|---|
| `foundry.run.started` | run begins | `run_id`, `project`, `system_version`, `pin_set_hash`, `started_at`, `worker_id`, `batch_id?` |
| `foundry.run.completed` | run ends successfully | + `total_duration_ms`, `total_cost_estimate_usd`, `total_input_tokens`, `total_output_tokens`, `total_reasoning_tokens` |
| `foundry.run.failed` | run terminated with error | + `error_class`, `error_message`, `error_context` |
| `foundry.run.cancelled` | cooperative cancellation | + `cancellation_reason` |

### Agent + node lifecycle

| Event | When | Key attributes |
|---|---|---|
| `foundry.agent.started` | agent step begins | `run_id`, `agent`, `agent_version` |
| `foundry.agent.completed` | agent returns | + `output_summary?`, `iteration_count`, `tokens_used` |
| `foundry.function_node.started` | function-node step begins | `run_id`, `node_name`, `node_version` |
| `foundry.function_node.completed` | function returns | + `fields_written`, `bytes_delta`, `latency_ms` |

### LLM calls

| Event | When | Key attributes |
|---|---|---|
| `foundry.llm.started` | provider call begins | `run_id`, `agent`, `provider`, `model`, `prompt_tokens_estimate?` |
| `foundry.llm.delta` | streaming chunk | `run_id`, `agent`, `content_block_index`, `delta` |
| `foundry.llm.completed` | provider call ends | + `usage` (`input_tokens`, `output_tokens`, `cached_read_tokens`, `cached_write_tokens`, `reasoning_tokens`), `cost_estimate_usd`, `latency_ms`, `stop_reason` |

### Tool + connection

| Event | When | Key attributes |
|---|---|---|
| `foundry.tool.started` | tool dispatch begins | `run_id`, `agent`, `tool_ref`, `tool_version`, `input_hash`, `input_preview?` |
| `foundry.tool.completed` | tool returns | + `success`, `latency_ms`, `retry_count`, `output_preview?`, `error_category?`, `connections_used`, `dangerous?` |
| `foundry.connection.acquire` / `cache_hit` / `refresh` / `release` / `evict` / `health_check` | connection-pool lifecycle | `run_id`, `connection_ref`, `connection_version`, `slot`, `auth_scheme`, `principal?` (redacted), `event` (lifecycle), `latency_ms`, `config_hash`, `error_category?` |

### Caching + retrieval + memory

| Event | When | Key attributes |
|---|---|---|
| `foundry.embed` | embedder call | `run_id`, `agent`, `embedder`, `model`, `input_count`, `input_tokens`, `purpose`, `latency_ms`, `cost_estimate_usd?` |
| `foundry.cache.semantic.hit` / `miss` / `store` / `invalidate` | semantic cache lifecycle | `run_id`, `agent`, `event`, `similarity?`, `threshold?`, `ttl_s?`, `saved_tokens_estimate?`, `saved_cost_usd?`, `cached_at?` |
| `foundry.cache.tool.hit` / `miss` / `store` | tool-result cache lifecycle | `run_id`, `agent`, `tool_ref`, `tool_version`, `event`, `input_hash`, `cached_at?` |
| `foundry.retrieval` | retriever call | `run_id`, `agent`, `retriever`, `kind` (`dense`/`sparse`/`hybrid`), `top_k`, `returned`, `latency_ms` |
| `foundry.rerank` | reranker call | `run_id`, `agent`, `reranker`, `model`, `candidates`, `top_k?`, `latency_ms`, `cost_estimate_usd?` |
| `foundry.memory.read` / `write` / `consolidate` | memory layer lifecycle | `run_id`, `agent`, `layer_name`, `layer_kind`, `event`, layer-specific fields |

### Orchestration

| Event | When | Key attributes |
|---|---|---|
| `foundry.handoff` | flow transition | `run_id`, `from_agent`, `to_agent`, `trigger` (`rule`/`llm`/`end`), `hop_number`, `state_size_bytes` |
| `foundry.state.transition` | state mutation | `run_id`, `agent`, `fields_written`, `bytes_delta` |
| `foundry.approval.required` / `resolved` / `escalated` | HITL lifecycle | `run_id`, `approval_id`, `agent`, `prompt`, `decision?`, `reason?`, `resolved_by?`, `timeout_s?` |

### Eval + forge

| Event | When | Key attributes |
|---|---|---|
| `foundry.eval.started` / `completed` | eval harness invocation | `eval_run_id`, `project?`, `target_ref`, `eval_spec_hash`, `cases_total`, `cases_passed?`, `score?` |
| `foundry.eval.case` | per-case result | `eval_run_id`, `case_id`, `score`, `pass`, `duration_ms`, `cost_usd?` |
| `foundry.eval.scheduled` (NEW per `41`) | scheduled re-eval fires | `project`, `eval_spec_hash`, `score`, `prior_score`, `delta`, `regression_threshold_breached?` |
| `foundry.forge.iteration_completed` | one iteration of forge loop | `forge_run_id`, `project`, `iteration_number`, `cluster_id?`, `change_kind`, `eval_delta`, `applied`, `commit_sha?` |
| `foundry.forge.terminated` | forge run ends | `forge_run_id`, `termination_reason`, `final_score`, `iterations`, `total_cost_usd`, `duration_s` |

### Versioning + audit

| Event | When | Key attributes |
|---|---|---|
| `foundry.rollback` | rollback applied | `project`, `granularity` (`tool`/`prompt`/`project`), `target_ref`, `from_version`, `to_version`, `commit_sha`, `audit_entry_id`, `operator`, `overrides_used?` |
| `foundry.catalog.promote` | catalog promotion | `from_project`, `artifact_ref`, `from_version`, `to_version`, `eval_score`, `breaking_changes?`, `operator` |
| `foundry.guard_finding` | parallel guard fired | `run_id`, `guard`, `event_triggered`, `verdict`, `cancelled_run?` |

### Connections + capabilities (system events)

| Event | When | Key attributes |
|---|---|---|
| `foundry.system.startup` | foundry-serve / process start | `worker_id`, `framework_version`, `project`, `system_version` |
| `foundry.system.shutdown` | graceful shutdown begins | `worker_id`, `phase`, `in_flight_runs` |
| `foundry.observability.degraded` | local audit / OTel exporter failure | `worker_id`, `subsystem`, `error` |
| `foundry.rate_limit` | rate-limiter event | `key` (`provider:model`), `event` (`granted`/`deferred`/`exceeded`), `wait_ms?` |
| `foundry.circuit_breaker` | circuit-breaker state change | `target`, `state` (`open`/`closed`/`half_open`), `failure_count?` |
| `foundry.api.request` (FastAPI) | HTTP request lifecycle | `request_id`, `path`, `method`, `status`, `duration_ms` |

This is the complete catalogue. The pattern: every framework-managed action is observable; every event has a `run_id` (or equivalent root identifier); every event timestamps + sequences.

## The three transports

### 1. OTel traces (debugging)

Spans for every event with parent-child relationships. The trace tree for one run:

```
foundry.run                              (root span; duration of whole run)
├── foundry.agent (orchestrator)
│   └── foundry.llm                      (orchestrator's LLM call)
│       └── foundry.handoff              (orchestrator → break_detector)
├── foundry.agent (break_detector)
│   ├── foundry.llm
│   ├── foundry.tool (query_snowflake)   (tool dispatch span)
│   │   ├── foundry.connection (acquire)
│   │   └── foundry.cache.tool (miss)
│   └── foundry.handoff (break_detector → orchestrator)
├── foundry.agent (orchestrator)
│   └── foundry.llm
└── ... (more iterations)
```

Tracing setup: `OpenTelemetry SDK` configured at startup; OTLP exporter (HTTP/gRPC) sends to your collector. Sample rate configurable (`ObservabilityConfig.sample_rate`); typically 1.0 in dev, 0.1–1.0 in prod depending on volume.

Backends: any OTel-compatible (Datadog APM, Jaeger, Tempo, Honeycomb, X-Ray, Langfuse Tracing).

### 2. OTel metrics (monitoring)

Counters, histograms, gauges aggregated over time windows. The full metric catalogue:

| Metric | Type | Dimensions |
|---|---|---|
| `foundry.run.total` | counter | `project`, `status`, `worker_id`, `batch_id?` |
| `foundry.run.duration_ms` | histogram | `project`, `worker_id` |
| `foundry.run.cost_usd` | counter | `project`, `worker_id`, `model` |
| `foundry.run.input_tokens` / `output_tokens` / `reasoning_tokens` | counter | `project`, `provider`, `model` |
| `foundry.llm.calls_total` | counter | `provider`, `model`, `agent` |
| `foundry.llm.latency_ms` | histogram | `provider`, `model` |
| `foundry.llm.cost_usd` | counter | `provider`, `model` |
| `foundry.tool.calls_total` | counter | `tool_ref`, `tool_version`, `success` |
| `foundry.tool.latency_ms` | histogram | `tool_ref`, `tool_version` |
| `foundry.tool.failure_rate` | derived | `tool_ref` |
| `foundry.connection.acquire_total` / `cache_hit_total` / `refresh_total` | counter | `connection_ref`, `connection_version` |
| `foundry.connection.health_ok` | gauge | `connection_ref` |
| `foundry.cache.semantic.hit_rate` | derived | `agent`, `project` |
| `foundry.cache.semantic.saved_cost_usd` | counter | `agent`, `project` |
| `foundry.cache.tool.hit_rate` | derived | `tool_ref` |
| `foundry.embed.cost_usd` | counter | `provider`, `model` |
| `foundry.retrieval.latency_ms` | histogram | `retriever`, `kind` |
| `foundry.rerank.latency_ms` / `cost_usd` | histogram / counter | `reranker`, `model` |
| `foundry.handoff_total` | counter | `from_agent`, `to_agent`, `trigger` |
| `foundry.eval.runs_total` | counter | `project`, `target_ref` |
| `foundry.eval.score` | gauge | `project`, `target_ref`, `eval_spec_hash` |
| `foundry.forge.iterations` | counter | `project`, `forge_run_id` |
| `foundry.rollback.total` | counter | `project`, `granularity` |
| `foundry.worker.concurrent_runs` (per `85`) | gauge | `worker_id`, `project` |
| `foundry.worker.event_loop_lag_ms` (per `85`) | histogram | `worker_id` |
| `foundry.worker.pool_wait_ms` (per `85`) | histogram | `worker_id`, `connection_ref` |
| `foundry.batch.in_flight` (per `85`) | gauge | `batch_id` |
| `foundry.batch.cost_usd` (per `85`) | counter | `batch_id` |
| `foundry.rate_limit.deferred_ms` (per `85`) | histogram | `provider`, `model` |
| `foundry.circuit.state` (per `85`) | gauge | `target` |

Backends: Prometheus, Datadog, CloudWatch, Stackdriver, NewRelic — anything OTLP-compatible.

### 3. Local SQLite event-mirror (dev queries)

For development workflows where running an OTel collector is overkill, the foundry mirrors the same events into `~/.foundry/observability.db`:

```
TABLE runs (run_id, project, system_version, started_at, completed_at, status, total_cost_usd, ...)
TABLE llm_calls (run_id, agent, provider, model, prompt_tokens, output_tokens, latency_ms, cost_usd, ...)
TABLE tool_calls (run_id, agent, tool_ref, tool_version, success, latency_ms, retry_count, ...)
TABLE handoffs (run_id, from_agent, to_agent, trigger, hop_number, ...)
TABLE evals (eval_run_id, project, target_ref, score, threshold, passed, ...)
TABLE forges (forge_run_id, project, termination_reason, final_score, iterations, ...)
TABLE rollbacks (timestamp, project, granularity, from_version, to_version, ...)
```

`foundry obs` queries hit this DB directly. No external dependencies; no service to run; queries are fast for a single user's history.

Schema versioned via `schema_version` column; migrations on framework upgrade.

## `foundry obs` CLI surface

```
foundry obs cost --project pipeline_recon [--since 7d] [--by model|day|agent]
foundry obs latency --project pipeline_recon [--p50|--p95] [--agent investigator]
foundry obs failures --project pipeline_recon [--since 1d] [--by error_class]
foundry obs tool-failures [--project pipeline_recon] [--by tool_ref]
foundry obs cache-stats --project pipeline_recon
foundry obs connections [--project pipeline_recon]
foundry obs eval-trend --project pipeline_recon [--since 30d] [--check-regression --threshold-drop 0.03]
foundry obs forge --project pipeline_recon [--since 30d]
foundry obs forge <forge_run_id>
foundry obs trace <run_id>                 # render OTel trace tree
foundry obs audit <project> [--since 7d] [--type rollback|forge|human]
foundry obs guards <project> --since 7d
foundry obs rollbacks <project> --since 30d
foundry obs runs <project> [--status active|completed|failed]
foundry obs query "SELECT ..."             # raw SQLite query (advanced)
```

Output: tabular by default; `--json` for machine-readable. Examples in `40-eval-harness.md` § Reporter (CLI table format).

## Backend integration patterns

### LangSmith (opt-in)

```bash
FOUNDRY_TRACING=langsmith
LANGSMITH_API_KEY=...
LANGSMITH_PROJECT=pipeline_recon
```

Routes traces to LangSmith's tracing UI. Useful for teams already using LangSmith for prompt/agent debugging.

### Langfuse (opt-in)

```bash
FOUNDRY_TRACING=langfuse
LANGFUSE_HOST=https://cloud.langfuse.com
LANGFUSE_PUBLIC_KEY=pk-...
LANGFUSE_SECRET_KEY=sk-...
```

LLM-native observability; integrates with their dashboards.

### Datadog APM

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://datadog-agent.internal:4318
DD_SERVICE=foundry-pipeline-recon
DD_ENV=prod
```

Standard Datadog setup; foundry's traces appear under your service.

### Self-hosted (Tempo / Jaeger / Loki / Prometheus / Grafana stack)

```bash
OTEL_EXPORTER_OTLP_ENDPOINT=http://otel-collector.internal:4317
```

The OTel collector forwards to your chosen storage. Standard CNCF observability stack.

## Privacy + redaction

`ObservabilityConfig` (per `12-config-and-validation.md`) controls what's captured:

```yaml
observability:
  trace: otel
  sample_rate: 1.0
  capture_inputs: true                    # tool/agent inputs in events
  capture_outputs: true
  capture_tool_args: true
  capture_state_diff: false               # state diffs in transition events; verbose
```

When `capture_inputs: false`, the `input_preview` / `output_preview` fields on `tool.started` / `tool.completed` are `None`. The hashes (`input_hash`, `output_hash`) remain — enabling correlation without leaking content.

Field-level redaction (per `52-rollback-and-audit.md` § Audit + per `23-connections-and-auth.md`):

- `ConnectionDescriptor.redacted_config` is the only connection metadata exposed to spans.
- Tool input/output previews are truncated to 500 chars + scanned for secret-shaped content.
- The redactor's denylist (`api_key`, `password`, `secret`, `token`, `private_key` field names) drops these fields from span attributes.
- A contract test asserts no known fake key (placed in test fixtures) appears in exported spans.

For PHI / MNPI / GDPR-personal-data: configure `capture_inputs: false` AND `capture_outputs: false` AND `capture_tool_args: false`; only structural metadata (timings, costs, success flags) goes to observability.

## Cost attribution

Every LLM call carries `cost_estimate_usd` (per `11-provider-abstraction.md` § Cost estimation). Aggregations:

- **Per project per day**: standard dashboard query.
- **Per agent**: which agent in the project drives spend.
- **Per model**: cost-mix across providers (informs cheaper-model substitution decisions).
- **Per cost_category** (optional `metadata.cost_category` on `SystemSpec`): chargeback to teams / desks.
- **Per batch**: enforced via `Session.cost_budget` + per-batch counter; observability surfaces actuals.
- **Per forge**: forge cost = sum of LLM calls during that forge_run_id.

The `foundry obs cost` CLI builds these queries; dashboards consume metrics directly.

Cost is indicative, not authoritative. The provider's invoice is canonical; foundry's pricing manifest can drift if pricing changes upstream. Operators reconcile periodically.

## Drift detection observability hooks (per `41`)

The `foundry.eval.scheduled` event — emitted when a CI cron / scheduled re-eval fires — is the foundation for drift dashboards:

```
Score over time per project:
  pipeline_recon: 0.91 (Apr-W1) → 0.91 (W2) → 0.89 (W3) → 0.85 (W4) ← drift!

Alert config:
  if score drops > 0.03 vs 30-day moving average → page operator
```

Standard observability backends handle the alerting. Foundry's job is providing the events with the right dimensions; alert thresholds + escalation paths are operator concerns.

## Failure modes

| Cause | Surfaced as |
|---|---|
| OTel collector down | events buffered up to a configurable cap; `foundry.observability.degraded` event; eventual drop with logged warning |
| Local SQLite store full / locked | events queue in memory; metric alert; may eventually drop |
| Audit store (separate from observability) down | per `52-rollback-and-audit.md` (audit failures don't block runs) |
| Span attribute exceeds OTel size limits | truncated with `_truncated: true` flag |
| Sensitive content slipping through redaction | contract tests catch known patterns; manual review of fixtures |

## Invariants

1. **Every event has a `run_id`** (or root identifier like `forge_run_id`, `eval_run_id`).
2. **Every event has a `timestamp` and (where applicable) a `sequence`**.
3. **Attribute schema is frozen per major foundry version**; additions OK; renames/removals require major bump.
4. **Three transports stay consistent**: an event in OTel traces, OTel metrics, and SQLite mirror reflects the same data.
5. **Field-level redaction is opt-out, not opt-in**: known sensitive field names are dropped by default.
6. **`foundry obs` queries the SQLite mirror** for low-overhead local queries; not the OTel backend.

## Test expectations

### Unit

1. **Event shape conformance**: every emitted event matches its declared `RunEvent` Pydantic schema.
2. **Attribute completeness**: every required attribute populated; optional ones present where applicable.
3. **Redaction**: known sensitive field names (`api_key`, `password`, `secret`, etc.) dropped from exported spans.
4. **SQLite mirror**: events written produce queryable rows; `foundry obs` returns expected data.

### Contract

1. **No credential leaks**: a fixture with known fake credentials run end-to-end; OTel + SQLite + run artifact scanned; no key found.
2. **Schema stability**: the frozen contract lives in [80-observability-attributes.yaml](80-observability-attributes.yaml); `tests/contract/test_observability_schema.py` parses it and compares against every event's field set and every mirrored span's attributes; CI fails on unintended changes.

### Integration (Phase 9 exit gate)

1. End-to-end run produces full event stream visible in OTel collector + SQLite mirror.
2. `foundry obs cost --project pipeline_recon --since 1d` returns numbers consistent with sum of `llm.completed.cost_estimate_usd` events.
3. LangSmith opt-in: configure key; traces appear in LangSmith UI.
4. Sustained-load test (per `85`): observability captures all events without drops; `foundry.observability.degraded` not emitted under nominal load.

## Open questions

1. **Sampling strategies**. Default 1.0 sample rate captures everything; some prod workloads need head-based or tail-based sampling. Lean: ship `sample_rate: 0.1` as a config knob; tail-based (sample failed runs at higher rate) is v1.1+.
2. **Custom event types from project code**. Tool handlers might want to emit project-specific events. Lean: add `ctx.session.emit_custom_event(name, attributes)` helper; events go through standard exporter; namespaced under `custom.<project>.<event>`.
3. **OpenTelemetry semantic conventions for AI/ML**. The OTel community is evolving conventions for LLM spans. Lean: align where stable; document deviations.
4. **Observability storage retention** — already in `81-storage-and-artifacts.md`; cross-reference rather than duplicate.
5. **SLO dashboards** as templates per project. Useful for institutions onboarding observability. Lean: ship sample Grafana JSON dashboards in `catalog/dashboards/` (analogous to catalog templates). v1.1+ deliverable.
