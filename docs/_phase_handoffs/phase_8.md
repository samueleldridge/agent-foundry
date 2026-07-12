# Phase 8 handoff — API layer + streaming + scaling + async polish

**Session date:** 2026-07-12
**Branch:** `main`
**Status:** Phase 8 implementation complete; awaiting AI review + operator
manual smoke test (docs/_manual_tests/phase_8.md). No live API keys, no
Redis, no live uvicorn in the dev sandbox — every gate below was verified
against `httpx.MockTransport` (LLM layer) and starlette's `TestClient` /
manual-lifespan ASGI runs against the REAL FastAPI app; the Redis token
bucket ran against an in-test fake implementing the one operation the
bucket uses (`eval` of the refill-and-take script) with the same atomic
semantics.

## Pre-work landed first (Phase 7 re-review finding)

`fix(orchestration)`: the predicate sandbox now forbids ANY
underscore-leading attribute anywhere in a chain (not just dunders) —
`state._data` read the StateProxy's own slot (resolved before
`__getattr__`), handing predicates the raw mutable state dict.
Adversarial matrix extended.

## Dependency decisions (also in the pyproject comment block)

- **fastapi >=0.139,<1** + **uvicorn >=0.51,<1** — range pins per the
  repo policy (exact pins are reserved for adapter-contract deps like
  langgraph/OTel); capped <1 against the eventual 1.0 re-architectures.
- **wsproto >=1.2** — uvicorn's pure-Python WS backend; chosen over
  `uvicorn[standard]` to avoid compiled extras.
- **redis / asyncpg deliberately NOT pinned** — the Redis rate limiter
  lazy-imports `redis.asyncio` and FAILS CLOSED with a structured
  `ProviderUnexpectedError(context={"rate_limiter": "unavailable"})`
  when missing/unreachable (same optional-backend policy as
  foundry.cache). Installing redis-py is a deploy-time choice.
- starlette 1.3 deprecates its httpx-backed TestClient in favour of the
  (unpinned) `httpx2` package; pytest filters that one warning. Swap
  when a later phase bumps starlette.

## What this session built

1. **Groundwork** — `_RunEventBase.worker_id` (hostname:pid, stamped by
   the runtime `EventEmitter`; default `""` so old artifacts parse);
   `InitRun` joined the `InboundMessage` union (docs/70 § WebSocket
   init); `llm_round` now emits `llm.delta` between `llm.started` and
   `llm.completed` (one synthesized delta per text block — see
   deviation 2).
2. **`foundry.providers.rate_limit`** — `RateLimiter` protocol;
   `InProcessTokenBucket` (per-process, per-key); `RedisTokenBucket`
   (docs/85 key pair, atomic Lua refill-and-take, bounded
   sleep-and-retry, fail-closed). `ProviderAdapter.generate` acquires a
   permit keyed `<provider>:<model>` per call; deferred waits poll the
   session cancel token (cancellation wins over backoff, docs/71).
   Selection: `FOUNDRY_RATE_LIMITER` (unset/off → no gate — the Phase ≤7
   behaviour; `in_process`; `redis://…`) + `FOUNDRY_RATE_LIMIT_RPS` /
   `_BURST`. Per-(provider,model) rate manifests are v1.1+.
3. **`foundry.api.runs.RunManager`** — the heart. Runs drive
   `run_project` as children of the app-lifespan `anyio` task group
   (structured concurrency; the "service nursery" — nothing orphans at
   shutdown). The per-run sink persists through `RunArtifactWriter`
   (the SSE-replay substrate), mirrors in memory, fans out to
   subscriber queues (SSE/WS/batch all subscribe). HITL pauses park the
   drive loop on an inbox and CONTINUE the same sequence on
   `ApprovalResponse` (docs/32); metadata mirrors the CLI shapes so
   `foundry resume` / `approvals list` see API runs. Cancellation =
   cancel token + per-run `CancelScope` → `run.cancelled(reason)` +
   resumable metadata (checkpoints already durable at node boundaries).
   `max_wall_time_s` → `reason=timeout`. Drain per docs/71: wait up to
   `FOUNDRY_DRAIN_TIMEOUT_S`, then force-cancel `reason=worker_drain`.
4. **`foundry.api.schemas`** — ProjectInput = the start node(s)'
   required state reads (the compiled state model's `is_required()`
   fields ∩ start-node read scope); ProjectOutput = the terminal
   agent's output schema (primary-agent mirror); sequential pipelines
   return the compiled state model (deviation 3).
5. **`foundry.api.routes` + `app`** — the full docs/70 catalogue
   attached with the derived models (`/openapi.json` IS the SystemSpec
   contract; deterministic per CompiledProject): `POST /run` (409 +
   pending approval on HITL; 499 on cancel; 200+status=failed for
   CostBudgetExceeded/output-validation per docs/70 § Failure modes),
   `POST /stream`, `POST /batch`, `WS /ws`, `GET /runs/{id}`,
   `GET /runs/{id}/events?from_sequence=N` (+ `Last-Event-ID`),
   `POST /runs/{id}/resume` (approval_response / resume / cancel /
   pause), `GET /health` (+`?deep=true` readiness; draining → 503),
   `GET /config` (redacted snapshot). App factory: eager compile,
   lifespan nursery, `X-Foundry-*` + `X-Request-Id` headers middleware,
   CORS stub (off unless origins configured), structured error
   handlers (never a stack trace), `route_prefix` for docs/70
   versioning Pattern 2. `create_app_from_env` is the uvicorn factory
   for `--workers N` (project path via `FOUNDRY_SERVE_PROJECT`).
6. **Auth** — `AuthBackend` Protocol; `BearerTokenAuth`
   (`FOUNDRY_API_TOKENS`, constant-time compare); `NoAuth` refuses
   under `FOUNDRY_ENV=prod`. Mounted on everything except `/health` +
   FastAPI's own schema/docs routes. WebSocket handshakes authenticate
   explicitly against the same backend (the HTTP `Request` dependency
   can't inject on websockets); rejects close with 1008.
7. **`foundry.api.streaming`** — SSE encoder (`id:` = sequence);
   `subscribe_events` = artifact replay → live handover, deduped by
   sequence (docs/85: SSE resume is worker-agnostic — any worker can
   replay from the shared FOUNDRY_HOME artifact); `sse_run_stream` ends
   at the terminal event and cancels the run from its `finally` when
   the client disconnects mid-run (`POST /stream` semantics). WebSocket
   handler: welcome frame (worker id, minted `next_run_id`), outbound
   `{"direction":"outbound","event":…}` frames, full inbound dispatch;
   client disconnect cancels in-flight socket-owned runs while
   approval-pending runs stay parked on the checkpointer.
8. **`foundry.api.batch`** — items are normal runs behind an
   `anyio.Semaphore(max_parallel)`; per-item events tagged
   `batch_id`/`item_id` over ONE SSE connection; the batch cost counter
   accumulates `llm.completed.cost_estimate_usd`; a breach emits
   `batch.budget_exceeded` BEFORE the cancellations it triggers
   (docs/85 invariant 8) and unstarted items fast-fail with synthetic
   `run.cancelled(reason=batch_budget_exceeded)`; per-item timeout →
   cancel(reason=timeout); terminal `batch.completed` summary. The
   executor runs on the app nursery and feeds a memory stream the
   response generator consumes — no task group suspended across a
   `yield` (the anyio async-generator pitfall), and a client disconnect
   tears the batch down through the closed stream.
9. **`foundry.api.worker`** — `worker_id` (shared accessor with
   tracing) + per-process `WorkerState` (uptime, draining flag).
10. **CLI** — `foundry serve <project> --host --port --workers N
    --checkpoint --route-prefix`; pre-flight compile for structured
    exit-2 errors; multi-worker via the uvicorn import-string factory.

## Deviations from the docs (all deliberate)

1. **In-process test transports buffer streaming bodies.** starlette's
   TestClient and httpx's ASGITransport both run the ASGI response to
   completion before yielding the first byte (measured), so
   "kill client mid-SSE" cannot be exercised over in-process HTTP. The
   test drives the exact layer starlette closes on a real disconnect —
   `sse_run_stream(...).aclose()` mid-stream — and the live curl+Ctrl-C
   variant is in the manual checklist (uvicorn propagates disconnects).
2. **`llm.delta` is synthesized (one delta per text block per call).**
   Provider adapters still complete calls via `generate()`; native
   incremental streaming (per-token deltas + mid-stream tool blocks) is
   the documented upgrade path inside the adapters — the wire contract
   (`llm.started` → `llm.delta`× → `llm.completed`) is already what
   docs/70 clients see. v1.1+ backlog.
3. **Sequential pipelines' ProjectOutput is the compiled state model** —
   `run_project` returns the FINAL STATE for sequential flows (a
   post-agent function node may transform the agent output), so the
   terminal agent's output schema would misdescribe the response.
   Single/supervisor/parallel/graph use the primary agent's schema.
   Graph multi-terminal discriminated unions (docs/70) remain ungated
   and unbuilt (Phase 7 deviation 11 still stands).
4. **Postgres checkpointer is documented, not shipped.** Checkpointer
   choices remain memory/sqlite/none (docs/71 table names Postgres for
   multi-worker prod; the runtime comment has always said "postgres
   lands with Tier 7 work" — it did not, and building a LangGraph
   PostgresSaver bridge was out of Phase 8's listed deliverables).
   Consequence: `foundry serve --workers N` requires sqlite and ONE
   host (shared FOUNDRY_HOME); WS stickiness across workers relies on
   the LB run_id-hash pattern (docs/85 Strategy 1, documented in the
   manual checklist). Cross-host resume is blocked until Postgres.
5. **`inject_input` into a RUNNING graph is refused** (structured error
   frame). v1 semantics: on a socket with no active run it STARTS the
   next run (JSON text → input object; plain text fills a
   single-required-field input) — which is what makes the exit gate
   ("inject reflected in subsequent output") true end-to-end. Mid-graph
   injection needs a runtime input channel (v1.1+ with chat-style
   continuations; Phase 7 deviation 3 territory).
6. **`pause` = checkpointed cancel** (`run.cancelled(reason="pause")`,
   status `paused`, resumable via `resume`); LangGraph has no true
   suspend short of the interrupt machinery. Approval timeouts
   (docs/32 `timeout_s`/`on_timeout`) remain unenforced — unchanged
   from Phase 7; nothing in the Phase 8 deliverable list covers them.
7. **Batch**: `streaming: false` (202 + poll, `GET /batches/*`,
   dead-letter store + `retry_failed`) is deferred with the batch
   store; the wire field parses and only `true` is honoured. The batch
   cost counter is in-process (a batch executes on the worker that
   accepted it); the docs/85 Redis counter is the multi-worker-fan-out
   scale-up path.
8. **API-layer consumer rate limiting** (docs/70 open q 5 "ship in
   Phase 8 polish") — not built; backpressure is
   `FOUNDRY_MAX_CONCURRENT_RUNS` + 503 Retry-After. The provider-side
   limiter landed per the deliverable list; consumer fairness limiting
   is v1.1+.
9. **Multi-project serving + header-versioned routing** (docs/70) are
   out per the deliverable list; `--route-prefix` covers URL versioning
   Pattern 2 (two processes).
10. **POST /run returns 200+status=failed only for
    CostBudgetExceeded/output-validation** (per the docs/70 table);
    other run failures map by error family (Provider* → 502,
    Connection*/Checkpoint* → 503, else 500) — the docs table's
    spirit, enumerated in `api/errors.py`.
11. **Adaptive rate-limit tightening + circuit breakers** (docs/85) are
    not in the Phase 8 deliverable list and were not built.

## Interface notes for Phase 9

- Every RunEvent now carries `worker_id`; the observability store can
  aggregate per-worker without joins. `foundry.api.worker.WorkerState`
  is where a heartbeat loop would attach (`foundry workers list`).
- The API layer's spans are NOT yet emitted (`foundry.api.*` middleware
  span is Phase 9 observability work); `foundry.run/node/llm` spans
  thread through unchanged, now with `worker_id` attributes.
- `RunManager` is the natural place for the SQLite event-mirror hook:
  `LiveRun.sink` already sees every event exactly once.
- The security-guardrails module (Phase 9) should wrap tool-output
  interpolation; nothing in the API layer interpolates tool output.
- Deployment (Dockerfile) can use `foundry serve` directly; the env
  manifest shape is in docs/85 § Example env manifest — of it, Phase 8
  honours FOUNDRY_RATE_LIMITER, FOUNDRY_MAX_CONCURRENT_RUNS,
  FOUNDRY_DRAIN_TIMEOUT_S, FOUNDRY_API_TOKENS, FOUNDRY_ENV,
  FOUNDRY_SERVE_PROJECT, FOUNDRY_CHECKPOINTER (memory/sqlite/none),
  FOUNDRY_CORS_ORIGINS, FOUNDRY_ROUTE_PREFIX.

## Exit-gate confirmation

| Gate | How verified | Outcome |
|---|---|---|
| serve hello + POST /run → Greeting | `test_post_run_round_trip_produces_a_greeting` (real app, ASGI TestClient, mock LLM); live curl in manual §1 | ✅ (mock) / ⏳ operator |
| OpenAPI == SystemSpec shapes, no hand-written routes | `test_openapi_schema_matches_systemspec_shapes` — HelloInput/Greeting exact; SAME factory yields TeamHelloInput/FinalSummary | ✅ |
| SSE progressive events, clean close | `test_post_stream_emits_progressive_run_events` — run.started → llm.delta → run.completed, id==sequence | ✅ |
| SSE reconnect Last-Event-ID N → replay N+1 | `test_sse_reconnect_...` — replay matches the continuous stream exactly (header + query forms) | ✅ |
| WS InjectInput → reflected; CancelRun → run.cancelled | `test_websocket_inject_input_reflected_in_output` (JSON + plain-text inject), `test_websocket_cancel_run_yields_run_cancelled` | ✅ |
| WS HITL approval flow | `test_websocket_hitl_approval_flow` — approval.required → ApprovalResponse → approval.resolved → run.completed(success), one contiguous sequence; plus non-streaming 409+/resume | ✅ |
| Kill mid-stream → cancel → status → resume | `test_kill_mid_stream_cancels_then_resume_completes` (generator-layer disconnect; see deviation 1) + manual §3 live | ✅ (see dev. 1) / ⏳ operator |
| Batch: 20 inputs, tags, budget | `test_batch_of_20_streams_tagged_per_item_events` + `test_batch_cost_budget_fast_fails_remaining_items` (breach precedes cancellations) | ✅ |
| Rate limiter: 3 workers share Redis bucket | `test_three_workers_share_one_redis_bucket_under_load` (fake Redis, atomic eval; aggregate ≤ rate×span+burst) + live-Redis manual §7 | ✅ (fake) / ⏳ operator |
| Sustained load | `test_sustained_load_no_dropped_events_sane_p95` — ~150 rps single worker for FOUNDRY_LOAD_TEST_DURATION_S (CI 8–10s), p95 ~150ms, contiguous artifacts, acquires==releases, over-budget fast-fail; full 100rps/5min recipe in manual §8 | ✅ (scaled) / ⏳ operator |
| Cancellation/timeout/drain polish | explicit cancel, wall-time timeout(reason=timeout), graceful drain completes in-flight, forced drain reason=worker_drain | ✅ |

## Definition-of-done confirmation

- `uv run ruff check src/ tests/` — zero violations.
- `uv run mypy --strict src/foundry/` — passes (173 files).
- `uv run pytest tests/` — **787 passed, 1 skipped** (751+1 pre-phase
  baseline intact — which was itself 744+1 plus the Phase 8 pre-work
  predicate tests — + 36 new).
- langgraph imports still confined to `langgraph_adapter.py` +
  `_langgraph_types.py`; `foundry.api` imports nothing from
  `foundry.configurator` (import-boundary lint + contract test).
- `run_id` + `worker_id` threaded through every event, span attribute,
  artifact, and response header.
- No secrets in code/configs/fixtures; bearer tokens only via env.
- Scope check: no OTel exporters/metrics store, no review TUI, no
  security-guardrails module, no deployment artifacts (all Phase 9).

**Phase 8 is COMPLETE pending review + operator manual smoke test. Next
session starts Phase 9 (observability + dev UX + security + deploy)
fresh.**
