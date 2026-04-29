# 71 — Async Runtime

## Purpose

This doc consolidates the foundry's async runtime semantics: the event-loop discipline, cancellation model, timeouts, structured concurrency, resumable runs via checkpointing, graceful shutdown, worker draining, and backpressure handling. Most of this is established in earlier docs (`10-core-framework.md` § Async runtime rules; `85-batch-and-throughput.md` § Scaling topology); this doc is the single place that explains the runtime as a coherent whole.

The discipline matters because the foundry runs against external systems (LLMs, databases, message queues) where every operation is bounded I/O. Done right, the runtime handles thousands of concurrent runs at predictable latency; done wrong, it leaks tasks, hangs on shutdown, or silently drops work.

Three load-bearing properties:

1. **Single event loop per process; `anyio` over `asyncio`.** No internal `asyncio.run()` calls; no thread pools other than `anyio.to_thread`; no callback-style scheduling. Standard, predictable, debuggable.
2. **Cancellation is cooperative + structured.** Every `await` either has a timeout above it or is inside a `CancelScope` that propagates cleanly. No orphan tasks; no zombie connections.
3. **State persists across process death.** Runs in flight survive worker restarts via the checkpointer. The async runtime + LangGraph adapter + checkpointer compose to make resumability mechanical.

## Module layout

```
src/foundry/runtime/
├── langgraph_adapter.py     ONLY place LangGraph is imported (per 02-framework-evaluation)
├── _langgraph_types.py      LG type ↔ foundry type conversions
├── checkpointers.py         in-memory / sqlite / postgres checkpointer selection
├── cancellation.py          CancelToken + scope plumbing
├── timeouts.py              per-call / per-run / per-batch timeout helpers
├── shutdown.py              graceful shutdown sequence
└── worker.py                worker identity + draining + heartbeat (per 85)
```

## Event loop discipline

### Rule 1: single event loop per process

The foundry never calls `asyncio.run()` internally. It accepts the event loop the caller provides:

| Caller | Loop source |
|---|---|
| CLI commands (`foundry run`, `foundry forge`, etc.) | `asyncio.run(main())` at the top-level entrypoint |
| FastAPI / `foundry serve` | uvicorn manages the loop |
| Jupyter notebooks | Jupyter's IPython kernel manages the loop |
| Tests | `pytest-asyncio` manages the loop |
| Library users | their own `asyncio.run()` or framework-provided loop |

A library that called `asyncio.run()` internally would clash with any of the above contexts. So the foundry's library API is `async def` everywhere; users are responsible for the event loop.

The CLI is the one exception — `foundry run hello ...` invokes `asyncio.run(_main(...))` exactly once. That's a top-level entrypoint, not library code.

### Rule 2: `anyio` over `asyncio` where possible

`anyio` is a dependency the foundry already takes (per `Phase 0` deliverables). Why prefer it:

- **Trio compatibility for free** — institutions running on trio aren't blocked.
- **`anyio.fail_after`** is a cleaner per-call timeout primitive than nested `asyncio.wait_for`.
- **`anyio.create_task_group`** is structured concurrency: an exception in one branch cleanly cancels siblings; no orphan tasks.
- **`anyio.to_thread.run_sync`** for running blocking code in a worker thread; first-class cancellation from the async side.

When `anyio` doesn't cover the use case (rare), fall back to `asyncio` directly. Document in code comments why.

### Rule 3: no blocking I/O in async code

Tool handlers, function-node bodies, and any other `async def` in the foundry MUST NOT make blocking calls (`requests.get`, `time.sleep`, `open(...).read()`, blocking database drivers). Either:

- Use an async-native client (`httpx.AsyncClient`, `asyncpg`, `aioboto3`).
- Wrap with `anyio.to_thread.run_sync(blocking_call, *args)`.

A lint rule (`flake8-async` or similar) catches the obvious cases (`requests.*`, `time.sleep`, `open` inside `async def`). Less obvious cases (a sync DB driver wrapped in a thin async facade) require code review.

### Rule 4: timeouts above every `await`

Every `await` in the foundry is bounded — either directly by `anyio.fail_after` or transitively because it's inside a function whose caller imposes a timeout. Unbounded waits are bugs.

Three layers of timeout:

| Scope | Layer | Default | Source |
|---|---|---|---|
| Per LLM call / streaming chunk | provider adapter | 60s | `ProviderAdapter` per `11-provider-abstraction.md` |
| Per tool call | tool dispatcher | 30s | `ToolSpec.timeout_s` per `20-tool-system.md` |
| Per agent invocation | agent dispatcher | derived from iteration_limit × LLM timeout | `AgentSpec.iteration_limit` |
| Per run | orchestration runtime | `Guardrails.max_wall_time_s` (project-level) | `SystemSpec.guardrails` |
| Per batch | batch executor | `BatchPolicy.per_item_timeout_s × max_parallel` | `85-batch-and-throughput.md` |

Whichever timeout fires first wins. The structured concurrency model means a parent timeout cancels child operations cleanly.

## Cancellation model

### Cooperative, structured, propagating

Cancellation in the foundry is **cooperative** (tasks must reach an `await` point to notice cancellation), **structured** (cancellation propagates from parent scopes to children via task groups), and **typed** (cancellation reasons are explicit, not just `CancelledError`).

The `CancelToken` (per `10-core-framework.md` § `CancelToken`):

```python
class CancelToken:
    def cancelled(self) -> bool: ...
    async def wait_cancelled(self) -> None: ...
    def cancel(self, reason: str) -> None: ...
    @property
    def reason(self) -> str | None: ...
```

The `Session.cancel_token` is the project's cancellation surface. When `cancel("...")` is called:

1. The underlying `anyio.CancelScope` is triggered.
2. All running `await`s within that scope raise `anyio.get_cancelled_exc_class()` (typically `Cancelled`).
3. The foundry's outer handler catches and re-raises as `RunCancelled` with the structured reason.
4. The orchestration runtime cleans up: pending tool calls cancel; in-flight LLM calls abort; checkpointer persists current state.

### Standard cancellation reasons

| Reason | Trigger |
|---|---|
| `user_abort` | `Ctrl-C` / explicit `foundry cancel` / API client disconnect |
| `timeout` | wall-clock exceeded `Guardrails.max_wall_time_s` |
| `max_hops_exceeded` | per `30-orchestration-patterns.md` |
| `max_iterations_exceeded` | per `21-agent-system.md` |
| `cost_budget_exceeded` | `Session.cost_budget.check` raised |
| `compliance_violation: <reason>` | guard observer detected violation (per `30` § Parallel guard) |
| `worker_drain` | k8s shutdown / `foundry workers drain` |
| `provider_failure` | upstream LLM or connection in unrecoverable state |
| `eval_infrastructure_failure` | eval harness can't run cleanly |
| `forge_terminated` | meta-agent's forge loop hit a termination condition |

The reason is recorded in `RunCancelled` event + audit log. Operators can query: "show me runs cancelled in the last 24h grouped by reason."

### Inter-task propagation

Within a `task_group`, cancelling one task cancels its siblings via `anyio`'s structured concurrency. Example: a parallel agent flow with three branches. Branch A fails; the task group's exception handler cancels branches B and C; the join receives partial state with the failure recorded.

```python
async with anyio.create_task_group() as tg:
    tg.start_soon(branch_a, state, session)
    tg.start_soon(branch_b, state, session)
    tg.start_soon(branch_c, state, session)
# When this block exits, all three are either completed or cleanly cancelled.
# No orphans.
```

The `failure_mode: cancel_siblings` (default for parallel patterns; per `30`) uses this. `failure_mode: collect_all` shields each branch with its own try/except so siblings continue.

### Cancellation is honoured even inside retries

The provider adapter's retry loop (per `11-provider-abstraction.md`) wraps `_stream_deltas` in a retry-with-backoff. If `cancel_token.wait_cancelled()` fires during a backoff sleep, the retry is abandoned — cancellation wins over backoff.

```python
async def _retry_loop(...):
    for attempt in range(max_attempts):
        try:
            async with anyio.fail_after(timeout_s):
                return await self._stream_deltas(...)
        except RetryableError:
            # Don't sleep through cancellation:
            with anyio.move_on_after(backoff_seconds):
                await session.cancel_token.wait_cancelled()
            if session.cancel_token.cancelled():
                raise RunCancelled(reason=session.cancel_token.reason)
            # Else: backoff completed without cancellation; retry
```

Cancellation as a first-class concern, not an afterthought.

## Structured concurrency for fan-out

### `anyio.create_task_group` everywhere fan-out happens

The foundry uses task groups for:

- Parallel agent nodes (per `30-orchestration-patterns.md` § Parallel pattern).
- Parallel tool calls within a single LLM turn (per `21-agent-system.md` § Multi-tool-call handling).
- Batch item execution (per `85-batch-and-throughput.md` § Batch executor).
- Parallel embedding batches (per `11-provider-abstraction.md` § Embedders).
- Parallel retriever branches in `HybridRetriever` (per `25-retrieval-and-rag.md`).

Each of these uses the same pattern: enter a task group, start child tasks, exit when all complete (or one fails and triggers cancellation of siblings).

```python
async with anyio.create_task_group() as tg:
    for branch in branches:
        tg.start_soon(execute_branch, branch, state, session)
# Either all branches completed or an exception propagated and siblings cancelled.
```

Benefits:
- No orphan tasks (a leaked `asyncio.create_task` is impossible inside the foundry).
- Predictable error propagation (exceptions surface; siblings clean up).
- Cancellation propagates from parent down (cancel the run → all in-flight branches cancel cleanly).

### Backpressure via bounded task groups

Some operations spawn many tasks (batch executor with N items). Without bounding, a 10k-item batch would create 10k task objects. The fix: `BoundedTaskGroup` wrapping `create_task_group` with a capacity semaphore:

```python
async with bounded_task_group(max_concurrent=32) as tg:
    for item in items:
        await tg.start_soon_when_ready(execute_item, item)
```

Internally: a semaphore with 32 permits; `start_soon_when_ready` waits for a permit before starting the task. Task completion releases the permit. Backpressure: if all 32 are busy, new spawns block.

`max_parallel` in `BatchPolicy` (per `85`) is exactly this. Standard pattern; documented but built on existing `anyio` primitives.

## Resumable runs

### The checkpointer integration

After every node boundary (per `01-architecture-overview.md` § Concurrency model + `31-multi-agent-systems.md`), the LangGraph runtime checkpoints the current state to the configured checkpointer. The checkpointer is provided by the runtime adapter:

| Backend | When | File |
|---|---|---|
| `MemoryCheckpointer` | tests; never prod | `runtime/checkpointers.py` |
| `SQLiteCheckpointer` | single-worker dev; default for `foundry run` | LangGraph's `SqliteSaver` |
| `PostgresCheckpointer` | multi-worker prod; recommended for any deployment with >1 worker | LangGraph's `PostgresSaver` |
| `RedisCheckpointer` | high-throughput prod (when latency matters more than durability guarantees) | optional |

Checkpointer is selected at startup via `FOUNDRY_CHECKPOINTER` env var (per `85`).

### Resume sequence

```
foundry run hello --resume <run_id>
   │
   ├── Resolve run_id → checkpoint key in checkpointer
   ├── Load checkpointed state
   ├── Reconstruct CompiledSystem from the run's recorded system_version
   │     (falls back to current if --use-current-version)
   ├── Mint new Session with same run_id (sequence continues from last persisted)
   ├── Resume execution at the next pending node
   ├── Continue normal RunEvent emission; sequence numbers increment from
   │   where the persisted artifact left off
```

Key invariant: a resumed run has the **same `run_id`** and **same `system_version`** as the original. Pin set is locked at compile time of the original run; rolling forward pins doesn't affect in-flight resumes.

### Cross-host resume

For multi-worker / multi-host deployments (per `85`):

- Postgres-backed checkpointer is the shared substrate.
- Run starts on worker A → state persists in Postgres.
- Worker A dies → no special handling needed.
- Resume request lands on worker B → worker B reads checkpoint from Postgres → continues.

Worker affinity is NOT required (in contrast to active WebSocket connections, which ARE worker-affined per `85` § Streaming under multi-worker). This is a deliberate design choice — the run state is in shared storage; any worker can pick up.

### Exception: HITL approval-pending runs

A run paused awaiting HITL approval (per `32-human-in-the-loop.md`) is a special case of "interrupted run." Same checkpointer; same resume; the inbound `ApprovalResponse` triggers continuation rather than a fresh resume. Behaviourally identical from the runtime's perspective.

## Graceful shutdown

When the foundry process receives `SIGTERM` (k8s shutdown, container restart, manual shutdown):

```
SIGTERM received
   │
   ├── 1. Mark process as draining (no new runs accepted)
   │     - /health endpoint returns 200 still (process alive)
   │     - /health?deep=true returns 503 with status "draining"
   │     - new POST /run / /stream / /batch return 503 + Retry-After
   │
   ├── 2. Wait for in-flight runs to complete (up to FOUNDRY_DRAIN_TIMEOUT_S)
   │     - default 120s
   │     - active streaming connections receive run.cancelled if not done
   │     - active WebSockets receive close frame
   │
   ├── 3. Force-cancel any remaining in-flight runs
   │     - all session.cancel_token.cancel("worker_drain") fires
   │     - run state checkpointed before final exit
   │     - (clients can resume on different workers if desired)
   │
   ├── 4. Close all connections in the pool
   │     - ConnectionPool.close_all() with per-connection timeout (10s)
   │     - audit log captures any forced-close
   │
   ├── 5. Flush observability
   │     - OTel exporter flush (with timeout)
   │     - audit log fsync
   │
   └── 6. Process exits cleanly
```

Per `85-batch-and-throughput.md` § Health endpoints and worker draining. The shutdown sequence is robust to common failure modes:

- If checkpointer is down at shutdown: in-flight runs are lost; metric alert fires; logs surface clearly.
- If a run has held a connection for hours and won't release: forced-close after the per-connection timeout; audit notes.
- If `SIGKILL` arrives before drain completes: state may be inconsistent; the next foundry instance discovers via checkpointer + reconciles.

### Hostile shutdown (operator-forced)

`foundry workers drain --force <worker_id>` skips the in-flight wait; immediately cancels all runs. Used for emergency rollouts. Logged loudly to audit.

## Worker draining + heartbeat

In multi-worker deployments (per `85`):

```python
# foundry/runtime/worker.py
async def worker_main():
    worker_id = f"{hostname}:{pid}"
    
    # Register self with TTL-based heartbeat:
    async with anyio.create_task_group() as tg:
        tg.start_soon(heartbeat_loop, worker_id, interval_s=10)
        tg.start_soon(run_uvicorn, app, host=..., port=...)
        tg.start_soon(signal_handler, on_sigterm=graceful_shutdown)
```

Heartbeat writes `foundry:workers:<worker_id>` in Redis (or equivalent) with TTL=30s. Other workers / load balancers can detect a dead worker by absence of heartbeat.

`foundry workers list` reads the heartbeat keys; shows live workers + their per-worker metrics (concurrent runs, event-loop lag, pool saturation).

## Backpressure handling

When the foundry is overloaded (more concurrent runs than capacity), it must shed cleanly rather than collapse. Backpressure mechanisms:

| Where | Mechanism |
|---|---|
| API layer | `FOUNDRY_MAX_CONCURRENT_RUNS` per worker; new requests get 503 with Retry-After |
| Provider rate limit | token-bucket gate (per `11`); new requests block on permit acquisition |
| Connection pool | `PoolPolicy.max_concurrent` per pool entry; `acquire` blocks on slot |
| Batch executor | `BatchPolicy.max_parallel` bounded task group |
| Memory cache | LRU eviction at `max_entries` |

Each layer has a clear "we're full" signal that propagates to the next outer layer. The API layer's 503 + Retry-After is the operator-visible signal; downstream load balancers / clients respect it.

### Adaptive degradation (deferred)

A natural enhancement: when overloaded, the foundry could shed lower-priority work first (cancel forge runs before user-initiated runs; pause batch jobs while real-time triage is hot). Today: no priority distinction; all runs treated equally. Marked as v1.1+ (priority-tier scheduling).

## Runtime observability

Every async-runtime concern is observable:

| Event / metric | Source |
|---|---|
| `foundry.runtime.event_loop_lag_ms` (histogram) | per `85-batch-and-throughput.md` worker metrics |
| `foundry.runtime.task_group_active` (gauge) | task groups currently in scope |
| `foundry.runtime.cancellation` (event) | `run.cancelled` with reason |
| `foundry.runtime.timeout` (event) | `RunFailed` with `error_class: TimeoutError` |
| `foundry.runtime.shutdown` (event) | shutdown sequence; phases logged |
| `foundry.runtime.pool_acquire_wait_ms` (histogram) | per `85-batch-and-throughput.md` worker metrics |

Operators can query: "what's our event-loop lag p95?" / "how often do we time out per project?" / "are we approaching pool exhaustion?" Standard observability stream feeds dashboards.

## Composition with other primitives

| Primitive | How runtime supports |
|---|---|
| `Session.cancel_token` | underpinned by `anyio.CancelScope`; reasons typed |
| `Session.cost_budget` | checked synchronously in provider adapter; doesn't block runtime |
| `CheckpointerHandle` | opaque wrapper around LangGraph's checkpointer; runtime never imports LG types |
| `RunEvent` stream | published from runtime hooks; consumed by API encoders + observers |
| `ConnectionPool` | acquires participate in cancellation; pool's own backpressure honoured |
| Batch executor | bounded task group; per-item timeout; cost budget shared |

The runtime is the integration substrate. Other primitives ride on top.

## Failure modes

| Cause | Surfaced as |
|---|---|
| Blocking call inside async code | (lint rule catches at code review); at runtime: event loop lag spike + observable degradation |
| Unbounded `await` | (lint rule + code review); runtime hang; healthcheck eventually fires |
| Task leak (orphan) | should be impossible with task groups; if it happens (operator code outside foundry's scope), event loop lag rises |
| Checkpointer unavailable | runs continue in memory; new requests get 503; metric alert |
| `SIGKILL` before drain | inconsistent state risk; next instance reconciles; some runs may need manual cleanup |
| Cancellation not honoured (badly-behaved tool) | tool's `timeout_s` fires; `ToolHandlerError(TimeoutError)`; run continues |
| Runaway cost (provider returns expensive responses repeatedly) | `Session.cost_budget` fires; `CostBudgetExceeded` halts cleanly |
| Provider streaming connection drops mid-response | provider adapter's retry policy retries OR raises `ProviderError`; run continues or fails based on policy |

## Invariants

1. **Single event loop per process**; no internal `asyncio.run()`.
2. **Every `await` has a timeout above it**, directly or transitively.
3. **Cancellation is cooperative, structured, and typed**.
4. **Task groups for all fan-out**; no `asyncio.create_task` in foundry code.
5. **Resume preserves identity**: same `run_id`, same `system_version`, sequence numbers continue.
6. **Pin set is locked at run compile time**, not at resume time.
7. **Graceful shutdown waits for drain timeout, then force-cancels with reason**.
8. **No blocking I/O in async code**; lint rule catches the obvious cases.

## Test expectations

### Unit

1. **Cancellation propagation**: nested task groups; cancel outer; all children cancel cleanly with the propagated reason.
2. **Timeout enforcement**: `anyio.fail_after(0.1)` around a 1s sleep raises `TimeoutError`.
3. **Backoff respects cancellation**: retry loop with 5s backoff; cancel at 1s; loop aborts at 1s, not 5s.
4. **Bounded task group**: 10 tasks with `max_concurrent=2`; only 2 ever active simultaneously.
5. **Checkpointer round-trip**: state → dump → load → equality.
6. **Resume preserves run_id + system_version**: kill mid-run; resume; assert identity.

### Contract

1. **Lint rule catches blocking imports**: `requests`, `time.sleep`, `open` inside `async def` flagged in CI.
2. **No orphan tasks after exception**: forced exception in a parallel branch; assert all sibling tasks reach completion or cancellation; no Python warnings about pending tasks.
3. **Graceful shutdown**: send SIGTERM; in-flight run completes (within drain timeout); process exits 0.

### Integration (Phase 8 exit gate)

1. End-to-end resume: kill `foundry serve` mid-run; restart; finish the run via `POST /runs/{id}/resume` (or it auto-resumes if connections come back).
2. Cross-host resume: 3-worker setup; run starts on worker 1; worker 1 killed; worker 2 picks up via Postgres checkpointer; run completes.
3. Sustained load: 100 concurrent runs/sec for 5 minutes; event-loop lag p95 < 50ms; no orphan tasks at end.
4. Hostile cancellation: kill -9 mid-run; restart process; checkpointer state recoverable; new run via resume completes.

## Open questions

1. **Trio support**. `anyio` makes this nearly free; do we test against trio or just asyncio? Lean: test against asyncio in v1; add trio CI when an institution actually wants it.
2. **uvloop vs the default loop**. `uvloop` is faster but Linux-only and less standard. Lean: don't bundle; institutions can opt in via their deploy config; document the env var.
3. **Adaptive degradation (priority-tier scheduling)**. Cancel forge runs before user-initiated runs under load. Lean: defer to v1.1+ (already in the v1.1 backlog memory).
4. **Native async DB drivers per connection kind**. Snowflake's connector is sync-friendly; needs `to_thread` wrapping. Some institutions prefer fully-async drivers. Lean: connection authors choose; the foundry's `Connection` protocol is async-native already.
5. **Per-run cost budget enforcement granularity**. Currently checked pre-LLM-call. Could check during streaming (cancel mid-response if cost exceeded). Lean: defer; pre-call check is cheap and rare overruns aren't worth the streaming complexity.
