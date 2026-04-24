# 85 — Batch and Throughput

## Purpose

The foundry is built to run agent *projects* in production, not just to prototype them. This doc specifies the scaling shape: how a configured project deploys as a multi-worker service that handles high-throughput batch processing and bursty real-time traffic cleanly, with trustworthy cost controls, end-to-end observability, and streaming that survives reconnects and worker loss.

The concrete use case shaping this spec is **post-trade operations at a hedge fund** — nightly reconciliation of 50k+ trades through an agent system; intraday triage of bursty exception queues with tight latency SLAs. The design generalises.

## What's in scope here

- Multi-process, multi-host deployment topology.
- Batch submission primitive (`POST /batch`).
- Cross-process rate limiting (Redis-backed token buckets).
- Circuit breakers for flaky dependencies.
- Dead-letter handling and batch retry semantics.
- Batch-level cost budgets.
- Worker identification and worker-tagged observability.
- Streaming under multi-worker (sticky WebSocket; SSE resume).

## What's NOT in scope

- Distributed task queues as a foundry feature (Celery/RQ/Arq). Users who need queue-based submission can put any queue in front of the foundry's HTTP API; the foundry doesn't ship one.
- Multi-region active/active. Multi-host within a region is supported; cross-region deployment requires shared-state considerations (checkpointer latency, Redis replication) the user owns.
- Auto-scaling policies. Kubernetes HPA, Nomad autoscale, etc. are orchestrator concerns; the foundry exposes the metrics that feed them.

## The scaling topology

```
                            ┌──────────────────────┐
                            │   Load balancer      │
                            │   (hash run_id       │
                            │    for WebSocket)    │
                            └──────────┬───────────┘
                                       │
             ┌─────────────────────────┼─────────────────────────┐
             │                         │                         │
             ▼                         ▼                         ▼
      ┌────────────┐           ┌────────────┐           ┌────────────┐
      │ Worker 1   │           │ Worker 2   │           │ Worker 3   │
      │ ─────────  │           │ ─────────  │           │ ─────────  │
      │ event loop │           │ event loop │           │ event loop │
      │ pools      │           │ pools      │           │ pools      │
      │ token cache│           │ token cache│           │ token cache│
      └─────┬──────┘           └─────┬──────┘           └─────┬──────┘
            │                        │                        │
            └────────────┬───────────┼────────────┬───────────┘
                         │           │            │
                         ▼           ▼            ▼
                    ┌─────────────────────────────────┐
                    │  Shared state                   │
                    │  - Postgres checkpointer        │
                    │  - Redis rate limiter           │
                    │  - Redis run registry           │
                    │  - Audit store (Postgres or S3) │
                    │  - OTel collector               │
                    └─────────────────────────────────┘
```

**Per-worker**: event loop, connection pools, token caches, LangGraph graph objects (compiled once per worker per project).
**Shared**: checkpointer, rate limiter, run registry, audit store, OTel stream.

Sizing heuristics for I/O-bound agent work:
- ~50–100 concurrent runs per worker comfortable.
- 2–8 workers per host typical (leaving headroom for CPU-bound scorers and tokenisers).
- Scale-out is adding hosts, not raising per-worker concurrency.

## Batch submission primitive

### Wire shape

```
POST /batch
Content-Type: application/json

{
  "batch_id": "<client-supplied or server-generated ULID>",
  "project": "pipeline_recon",
  "items": [
    {"item_id": "trade_001", "input": { ... }},
    {"item_id": "trade_002", "input": { ... }},
    ...
  ],
  "policy": {
    "max_parallel": 32,
    "max_cost_usd": 500.0,
    "per_item_timeout_s": 300,
    "stop_on_budget_exceeded": true,
    "stop_on_failure_rate": 0.5,
    "streaming": true
  }
}
```

Response is an SSE stream of `RunEvent`s, each tagged with `batch_id` + `item_id` in addition to its usual attributes. Terminal events per item are `run.completed` / `run.failed` / `run.cancelled`. Terminal event for the batch itself is a synthetic `batch.completed` that summarises pass/fail counts, total cost, and duration.

Non-streaming mode (`policy.streaming: false`) returns `202 Accepted` with a `batch_id`; caller polls `GET /batches/{batch_id}` for status and `GET /batches/{batch_id}/items` for per-item results.

### Batch executor

`foundry.api.batch.BatchExecutor` owns:
- A bounded task group (`anyio.create_task_group`) capped at `policy.max_parallel`.
- A cost-budget counter (shared via Redis for multi-worker deployments; per-batch key).
- A failure-rate tracker (rolling window; trips `stop_on_failure_rate`).
- A `batch_id`-prefixed event sink.

Per-item execution goes through the normal `foundry.orchestration` path — a batch item IS a run. Batch-level concerns (cost, failure rate, parallelism) wrap it.

### Dead-letter handling

Failed items (`run.failed`) collect into a per-batch dead-letter list accessible at `GET /batches/{batch_id}/dead_letter`. The caller decides whether to retry (manual replay, edit inputs, etc.). `POST /batches/{batch_id}/retry_failed` resubmits just the failed items into a NEW batch, with the original batch referenced for audit.

### Batch-level cost budget

The Redis counter `foundry:batch:<batch_id>:cost_usd` is incremented after every `llm.completed` event's `cost_estimate_usd`. Before starting a new item (and before each `llm.started` in an in-flight item), the executor checks the counter against `policy.max_cost_usd`. Breach outcomes:

- `stop_on_budget_exceeded: true` → do not start new items; in-flight items run to completion; emit `batch.budget_exceeded` event.
- `stop_on_budget_exceeded: false` → record the breach in the batch summary; continue.

For single-worker dev, the counter is in-process. For multi-worker, Redis is required — the counter is load-bearing for correctness.

### Per-item streaming in a batch

Per-item streaming can either multiplex over one SSE connection (default) or per-item WebSocket (opt-in via `policy.transport: "websocket"`). The multiplexed SSE form is preferred for throughput — one connection per batch is cheaper than N sockets.

## Cross-process rate limiting

Anthropic's org-level rate limit is a shared resource. Each worker's local retry loop doesn't know what the other workers are doing. A coordinated token-bucket limiter is required.

### Redis token bucket

Two Redis keys per (provider, model):

```
foundry:rl:<provider>:<model>:tokens   INT   current tokens available
foundry:rl:<provider>:<model>:last     INT   last refill unix-millis
```

`acquire(cost)` is a Lua script (atomic):
1. Compute elapsed time since `last`, add proportional tokens up to `capacity`, write back.
2. If `tokens >= cost`, decrement by `cost`, return "granted".
3. Else, return "deferred" with a recommended wait-ms.

Client (`foundry.providers.rate_limit.RedisTokenBucket`) does `fail_after`-bounded sleep-and-retry on `deferred`. Honours cancellation cleanly.

### Limit keys and rate sources

Keys are `<provider>:<model>`. Rates come from:
1. `ProviderCapabilities` manifest (baseline defaults).
2. `~/.foundry/rate_limits.yaml` (user overrides — required for orgs with elevated tier quotas).
3. `FOUNDRY_RATE_LIMITS_URL` (optional HTTP fetch; refreshes on interval).

### Adaptive tightening

Observed `ProviderRateLimitError`s (429s) feed `AdaptiveRateLimiter.report(was_throttled=True)`, which reduces the refill rate for a cool-down window. Prevents a tiered quota change at the provider side from requiring a config redeploy to see the benefit.

### Multi-region considerations

If workers deploy across regions each talking to the same provider org, one Redis instance is the coordination point. Latency matters — a Redis round-trip per LLM call adds ~1–3ms. That's acceptable for agent workloads (LLM latency is orders of magnitude higher). For strict latency budgets, use regional Redis with per-region token allotments.

## Circuit breakers

Wraps each `(provider, model)` in `foundry.providers` and each `(connection_ref, project)` in `foundry.connections`. Three states: CLOSED (normal), OPEN (fast-fail), HALF_OPEN (probe).

```python
class CircuitBreakerConfig(BaseModel):
    failure_threshold: int = 10
    failure_window_s: int = 60
    cool_down_s: int = 30
    probe_concurrency: int = 1
    enabled: bool = False    # opt-in
```

OPEN state fast-fails with `ProviderUnexpectedError(context={"circuit": "open"})` or `ConnectionAuthError(context={"circuit": "open"})` — clients don't hammer a downed dependency.

Backing store is Redis for prod (same rationale as rate limiter). In-process for dev.

## Worker identification and tagged observability

### `worker_id`

Format: `<hostname>:<pid>`. Registered on startup into Redis key `foundry:workers:<worker_id>` with TTL-based heartbeat.

Tagged into every `RunEvent`, every OTel span, every metric dimension.

### Worker-aware metrics

Beyond the baseline metrics in `80-observability.md`:

| Metric | Type | Dimensions |
|---|---|---|
| `foundry.worker.concurrent_runs` | gauge | `worker_id`, `project` |
| `foundry.worker.event_loop_lag_ms` | histogram | `worker_id` |
| `foundry.worker.pool_wait_ms` | histogram | `worker_id`, `connection_ref` |
| `foundry.batch.in_flight` | gauge | `batch_id` |
| `foundry.batch.cost_usd` | counter | `batch_id` |
| `foundry.rate_limit.deferred_ms` | histogram | `provider`, `model` |
| `foundry.circuit.state` | gauge | `target`, state (as attribute) |

### Health endpoints and worker draining

`GET /health` returns per-worker status: event-loop lag, pool saturation, in-flight runs, connection healths (cached). Orchestrators use it for readiness probes.

`POST /admin/drain` marks the worker as draining: no new runs accepted, in-flight runs allowed to complete up to a timeout, then process exits cleanly. Checkpointer preserves state; surviving workers pick up resumed runs.

## Streaming under multi-worker

### SSE: simple, worker-agnostic

SSE connections attach to a worker. If the worker dies, the client reconnects. Any surviving worker can:
1. Load the persisted `RunEvent` history from the run artifact.
2. Replay events with `sequence > Last-Event-ID`.
3. If the run is still active on another worker, subscribe to its live event stream via Redis pub/sub (`foundry:events:<run_id>` channel). Otherwise replay completes at the persisted terminal event.

This is the recommended pattern — no stickiness required.

### WebSocket: sticky by `run_id`

WebSocket needs to reach the worker currently executing the run, because inbound messages (`approval_response`, `inject_input`, `cancel`) must modify live state.

Two viable routing strategies:

**Strategy 1 — LB hash on `run_id`.** Load balancer routes WS connections whose URL carries `run_id` to the worker `hash(run_id) % N_workers`. Simple; fragile under worker scale-up/down.

**Strategy 2 — Run registry + proxy.** Redis hash `foundry:runs:<run_id> → worker_id`. Each worker writes itself when it accepts a run. Client connects to any worker; if that worker isn't the owner, it proxies the WS to the owner (or replies with a redirect directive the client follows). More robust; small amount of intra-cluster traffic.

Default v1: **Strategy 1** (simpler, good enough for most hedge-fund deployments). Strategy 2 documented as an option for operators who need it.

### What happens when the owning worker dies mid-WebSocket

Client sees the socket close. Reconnects. Strategy 1: new hash, new worker. Strategy 2: registry updated by the checkpointer's resumer. Either way:
- The run state is on the checkpointer.
- The client picks a strategy:
  - Downgrade to SSE resume with `Last-Event-ID` (reads artifact, subscribes to pub/sub).
  - Reconnect WebSocket to the new owner once the registry points there.

Client libraries the foundry ships should implement this transparently. In the interim, docs on the wire protocol should make it clear what servers do and what clients need to handle.

## Deployment reference configurations

### Single-worker dev

- `foundry serve <project>` (one uvicorn process).
- SQLite checkpointer.
- In-process rate limiter and circuit breaker.
- OTel exporter → console.
- No Redis dependency.

### Multi-worker, single-host staging

- `foundry serve <project> --workers 4`.
- Postgres checkpointer.
- Redis on localhost for rate limiter + run registry (optional run registry).
- OTel → OTLP endpoint (Langfuse, etc.).
- Batch budget tracking via Redis if batches are run against staging.

### Multi-worker, multi-host prod

- N hosts × M workers behind a load balancer.
- Postgres checkpointer (shared instance or replica).
- Redis cluster for rate limiter, run registry, circuit breaker, batch budget counters.
- OTel → enterprise collector.
- Audit store mirror in Postgres + object-store archive.
- LB routing: run_id hash for WebSocket (or optional proxy strategy); unstuck for SSE/POST.

### Example env manifest (multi-worker prod)

```
FOUNDRY_ENV=prod
FOUNDRY_CHECKPOINTER=postgres://...
FOUNDRY_RATE_LIMITER=redis://...
FOUNDRY_RUN_REGISTRY=redis://...
FOUNDRY_CIRCUIT_BREAKER=redis://...
FOUNDRY_AUDIT_STORE=postgres://...   # or s3://bucket/prefix
FOUNDRY_TRACING=otel                  # or 'langsmith'
OTEL_EXPORTER_OTLP_ENDPOINT=https://otel-collector.internal:4317
FOUNDRY_WORKER_ID=                    # auto if empty
FOUNDRY_DRAIN_TIMEOUT_S=120
FOUNDRY_MAX_CONCURRENT_RUNS=100       # per worker
FOUNDRY_SECRETS_PROVIDER=vault         # or aws, gcp, env
VAULT_ADDR=...
VAULT_TOKEN=...
```

## Invariants

1. **A batch's cost counter is correct across workers.** Tested under load with synthetic cost spikes.
2. **Rate limiter permits do not exceed the configured rate over any 60-second window, across all workers.** Tested with a multi-worker load generator.
3. **Worker death does not lose in-flight run state.** Runs resume on a different worker from the last checkpoint.
4. **`batch.completed` is emitted exactly once per batch.** Idempotent under worker churn; backed by a Redis SETNX.
5. **Event sequences are monotonic per `run_id` across the run's lifetime**, including across worker handoffs during resume.
6. **SSE `Last-Event-ID` replay produces the same sequence of events a naive listener would have seen**, modulo missed live-only events during the gap (which are replayed from the artifact).
7. **Circuit breaker state is shared when Redis-backed.** Workers agree on OPEN/HALF_OPEN/CLOSED.
8. **Batch policy breaches emit an event and update the artifact before any `run.failed`/`run.cancelled` they trigger.** Ordering matters for audit.

## Failure modes (beyond existing `FoundryError` subclasses)

| Cause | Surfaced as |
|---|---|
| Batch budget exceeded mid-batch | `batch.budget_exceeded` event; per-item `RunCancelled(reason="batch_budget_exceeded")` for items not yet started |
| Batch failure-rate tripped | `batch.failure_rate_tripped` event; same semantics as budget breach |
| Rate limiter unavailable (Redis down) | `ProviderUnexpectedError(context={"rate_limiter": "unavailable"})` — fail-closed to avoid runaway; alerting via metrics |
| Run registry unavailable (WS Strategy 2) | WebSocket connect receives `503` with `Retry-After` |
| Worker draining, run in flight | Run checkpoints; new worker resumes; client reconnects via SSE resume or WS re-routing |
| Postgres checkpointer latency spike | Runs continue; metric `foundry.checkpointer.latency_ms` alerts; no data loss |
| Audit store write fails | Metric + log; retry on next event; falls back to local-only if persistent — emits `foundry.observability.degraded` event |

## Test expectations

### Integration

1. **Batch cost budget**: submit a batch with max_cost_usd = $X; inject synthetic high-cost model responses; assert the budget is respected within tolerance (budget ± one in-flight item's cost).
2. **Shared rate limiter**: 3 workers + Redis bucket at rate R; sustained load; aggregate rate across workers does not exceed R over any 60s window.
3. **Worker kill during run**: submit a long-running run; kill owning worker mid-execution; assert new worker resumes from checkpoint; client's SSE reconnect replays cleanly.
4. **Circuit breaker**: configure target to fail 100% for 60s; observe breaker opens; probe after cool-down; breaker closes after successful probes.
5. **Dead-letter retry**: batch of 10 where 3 fail; `POST /batches/<id>/retry_failed` submits a new batch with the 3; audit trail links the two.

### Load test (Phase 8 exit gate)

1. **100 concurrent runs/sec for 5 minutes** against a trivial project across 4 workers sharing Redis + Postgres.
2. Assertions:
   - 0 dropped `RunEvent`s (every run has `run.started` matched with a terminal event).
   - p95 LLM latency within 2× of baseline.
   - 0 orphan connection-pool entries at test end.
   - `foundry.worker.event_loop_lag_ms` p95 under 50ms.
   - Batch-level cost budget enforced exactly; no overruns.

## Operational CLI surface (previewed; detail in `82-dev-ux.md`)

- `foundry batch submit <project> --items path/to/items.jsonl --max-parallel N --max-cost-usd X` — submit via CLI (wraps the HTTP API).
- `foundry batch status <batch_id>` — status + per-item summary.
- `foundry batch retry-failed <batch_id>` — resubmit failures as a new batch.
- `foundry obs load-test <project> --rps 100 --duration 5m` — built-in load-test harness (Phase 9).
- `foundry workers list` / `foundry workers drain <worker_id>` — admin commands.

## Open questions

1. **Batch-level retry policy primitives.** Should we offer declarative "retry idempotently up to N times on failure category X" at the batch level? Recommend: defer. Add to caller logic in v1; formalise if real use cases demand.
2. **Cost attribution beyond per-project.** Multiple projects share a token budget (e.g. an entire team). Lean: add `cost_category` as an optional field on `SystemSpec` + the batch request; metrics tag accordingly.
3. **Fairness across batches.** Two batches competing for the rate limiter — FIFO by acquire order? Weighted by priority? Lean: FIFO v1; priority tiers as an add-on.
4. **Drain with long-running HITL approvals.** A draining worker holding a run awaiting approval can block indefinitely. Add `FOUNDRY_DRAIN_ABANDON_AFTER_S=X` that cancels in-flight runs past X seconds of drain wait, with clear `run.cancelled(reason="worker_drain")`.
5. **WebSocket Strategy 1 vs 2 default.** Currently: Strategy 1 (LB hash) in v1. Worth measuring operational pain before committing forever. Lean: ship Strategy 1; add Strategy 2 if the hash-reshuffle-on-scale issue becomes real.
