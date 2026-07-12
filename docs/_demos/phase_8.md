# Phase 8 demo — the served system: HTTP, SSE, WebSocket, batch

Two ways to watch it: the NO-KEY demo (exit-gate integration tests
against the real FastAPI app with a mock LLM transport) and the LIVE demo
(real key + real uvicorn + curl/wscat — full checklist in
`docs/_manual_tests/phase_8.md`).

## Hero demo (no API key)

Everything except the LLM HTTP layer is real: real compile, real derived
OpenAPI models, real lifespan task group, real SSE/WS wire frames, real
sqlite checkpointer behind cancel/resume.

```bash
# 1) POST /run round-trip, OpenAPI == SystemSpec, auth, health, config:
uv run pytest tests/integration/test_api_hello.py -q

# 2) SSE progressive events + Last-Event-ID replay + kill-mid-stream →
#    cancel → resume + WS inject/cancel:
uv run pytest tests/integration/test_api_streaming.py -q

# 3) WebSocket HITL against team_hello (approval.required →
#    ApprovalResponse → approval.resolved → completed):
uv run pytest tests/integration/test_api_team_hitl.py -q

# 4) Batch: 20 items over one SSE connection + budget fast-fail:
uv run pytest tests/integration/test_api_batch.py -q

# 5) Sustained load (scaled) + graceful/forced drain; crank it with
#    FOUNDRY_LOAD_TEST_DURATION_S=30:
uv run pytest tests/integration/test_api_load.py -q -s

# 6) The shared token bucket: 3 workers, one (fake) Redis, aggregate
#    rate under the limit:
uv run pytest tests/unit/test_providers_rate_limit.py -q
```

The `-s` on (5) prints the observed throughput, e.g.:

```
sustained load: 1560 runs in 10s (156.0 rps), p95 154ms
```

## Live demo (key required, ~2 minutes)

```bash
export ANTHROPIC_API_KEY=...
uv run python -m foundry serve projects/hello --port 8080
```

```bash
# typed round-trip
curl -si -X POST http://127.0.0.1:8080/run \
  -H 'Content-Type: application/json' -d '{"name": "world"}' | head -12

# watch the run happen, event by event
curl -sN -X POST http://127.0.0.1:8080/stream \
  -H 'Content-Type: application/json' -d '{"name": "streamer"}'

# the schema a client would codegen against
curl -s http://127.0.0.1:8080/openapi.json | python -m json.tool | grep -A8 HelloInput
```

Expected SSE shape (ids = sequence numbers; clean close after the
terminal frame):

```
id: 0
event: run.started
data: {"run_id":"01…","sequence":0,…,"worker_id":"host:pid",…}

id: 2
event: llm.started
…
event: llm.delta
…
event: run.completed
data: {…,"status":"success","final_output":{"greeting":"…"}}
```

For the WebSocket HITL pause/approve flow, the batch stream, the
live-Redis rate limiter, and the full 100 rps × 5 min load recipe, follow
`docs/_manual_tests/phase_8.md` §§4–8.

**Outcome record (operator fills in):**

- Date: ____
- Manual checklist result: PASS / FAIL (notes: ____)
