# Phase 8 — Manual Smoke Tests

**Phase scope**: `foundry.api` (app factory, routes, SSE, WebSocket,
batch, auth, worker identity), `foundry.providers.rate_limit`,
cancellation/timeout/drain polish, `foundry serve`.

**Reference**: [docs/03-development-phases.md § Phase 8](../03-development-phases.md)
exit gate; [docs/_phase_handoffs/phase_8.md](../_phase_handoffs/phase_8.md)
for deviations (buffered test transports; synthesized single llm.delta;
Postgres checkpointer documented-not-shipped; inject-into-running-graph
deferred).

## Preconditions

- Phase 7 manual smoke test fully signed off.
- Claude Code review session for Phase 8 has reported **PASS**.
- Working tree clean; `uv run pytest tests/` green.
- A real `ANTHROPIC_API_KEY`. This sheet is cheap (~$0.20 at Haiku
  pricing) EXCEPT step 8's full load recipe (budget it deliberately).
- For steps 6–7: a local Redis (`brew install redis` / `docker run -p
  6379:6379 redis:7`) and `uv add redis` in a throwaway branch or
  `uv pip install redis` into the venv (redis-py is deliberately NOT a
  pinned dependency).

## Setup

```bash
cd /Users/sam/projects/agent-foundry
uv sync
export ANTHROPIC_API_KEY=...   # never echo it
rm -f ~/.foundry/checkpoints/hello.sqlite ~/.foundry/checkpoints/team_hello.sqlite
```

## 1. `foundry serve hello` + curl round-trip (exit gate 1)

```bash
uv run python -m foundry serve projects/hello --port 8080
# second terminal:
curl -si -X POST http://127.0.0.1:8080/run \
  -H 'Content-Type: application/json' -d '{"name": "world"}'
```

- [ ] `200` with a JSON `{"greeting": "..."}` mentioning "world" (and the
      current time via the catalog tool).
- [ ] Response headers carry `X-Foundry-Run-Id` (26-char ULID),
      `X-Foundry-System-Version`, `X-Foundry-Pin-Set-Hash`,
      `X-Foundry-Worker-Id` (`hostname:pid`), `X-Request-Id`.
- [ ] `curl -s -X POST .../run -d '{}' -H 'Content-Type: application/json'`
      → `400` naming the missing `name` field; no stack trace.
- [ ] `~/.foundry/runs/<RUN_ID>/events.jsonl` exists; every line carries
      `worker_id`.

## 2. OpenAPI is real (exit gate 2)

```bash
curl -s http://127.0.0.1:8080/openapi.json | python -m json.tool | less
```

- [ ] `components.schemas.HelloInput` requires exactly `name: string`,
      `additionalProperties: false`.
- [ ] `POST /run`'s 200 response schema is `Greeting`
      (`greeting: string`, required).
- [ ] The catalogue is complete: `/run /stream /batch /runs/{run_id}`
      `/runs/{run_id}/events /runs/{run_id}/resume /health /config`.
- [ ] Optional (client-codegen spot check):
      `npx openapi-typescript http://127.0.0.1:8080/openapi.json` emits a
      typed client without errors.

## 3. SSE stream + Last-Event-ID reconnect + kill-mid-stream (gates 3, 4, 7)

```bash
curl -sN -X POST http://127.0.0.1:8080/stream \
  -H 'Content-Type: application/json' -d '{"name": "streamer"}'
```

- [ ] Progressive frames: `run.started` → `agent.started` →
      `llm.started` → `llm.delta` (one per text block — synthesized;
      see handoff deviation 2) → `llm.completed` → tool events →
      `run.completed`; `id:` equals the event `sequence`; connection
      closes cleanly after the terminal frame.

Kill mid-stream + reconnect (use a run long enough to interrupt — the
tool round-trip gives you a beat, or throttle your terminal):

```bash
curl -sN -X POST http://127.0.0.1:8080/stream \
  -H 'Content-Type: application/json' -d '{"name": "cutoff"}' & sleep 0.4; kill %1
# note the run id from the frames you saw, then:
curl -s http://127.0.0.1:8080/runs/<RUN_ID> | python -m json.tool
curl -sN http://127.0.0.1:8080/runs/<RUN_ID>/events -H 'Last-Event-ID: 2'
curl -s -X POST http://127.0.0.1:8080/runs/<RUN_ID>/resume \
  -H 'Content-Type: application/json' \
  -d '{"kind": "resume", "run_id": "<RUN_ID>", "client_sequence": 0}'
```

- [ ] After the kill, `GET /runs/<RUN_ID>` shows `status: cancelled`
      (uvicorn propagates the disconnect; the server logs the cancel) —
      the artifact ends with `run.cancelled` `reason: user_abort`.
- [ ] The `Last-Event-ID: 2` replay starts at sequence 3 and replays the
      persisted events exactly.
- [ ] `POST .../resume {"kind": "resume"}` completes the run and returns
      the Greeting; `events.jsonl` shows ONE contiguous sequence across
      kill + resume (a second `run.started`, one `run.cancelled`, final
      `run.completed`).

## 4. WebSocket: inject → output; cancel (gate 5)

```bash
# npx wscat or websocat; wsproto is the server backend
npx wscat -c ws://127.0.0.1:8080/ws
> {"direction":"inbound","message":{"kind":"inject_input","run_id":"<welcome.next_run_id>","client_sequence":0,"message":{"role":"user","content":[{"type":"text","text":"{\"name\": \"Ada\"}"}]}}}
```

- [ ] A `welcome` frame arrives on connect (worker_id, project,
      next_run_id).
- [ ] After `inject_input`: outbound `run.started` … `llm.delta` frames
      whose text mentions **Ada**; `run.completed.final_output.greeting`
      reflects the injected input.
- [ ] Start another run, then send
      `{"kind":"cancel","run_id":"<id>","client_sequence":1}` while it's
      in flight → outbound `run.cancelled` with `reason: user_abort`.
- [ ] Send garbage (`{"kind":"nope"}`) → an `error` frame, socket stays
      open.

## 5. WebSocket HITL against team_hello (gate 6)

```bash
uv run python -m foundry serve projects/team_hello --port 8081
npx wscat -c ws://127.0.0.1:8081/ws
> {"direction":"inbound","message":{"kind":"init_run","client_sequence":0,"input":{"request":"the new release shipping","audience":"the team"}}}
```

- [ ] Handoff events stream (coordinator→drafter→coordinator→publisher).
- [ ] `approval.required` frame arrives (publisher, `publish-<run_id>-…`
      id, greeting text in the prompt) followed by
      `run.completed status=approval_pending`; the socket stays open.
- [ ] `GET /runs/<RUN_ID>` shows `approval_pending` + the payload; the
      run ALSO shows up in `uv run python -m foundry approvals list`
      (shared artifact surface).
- [ ] Send `{"kind":"approval_response","run_id":"<id>",
      "client_sequence":1,"approval_id":"<id>","decision":"approved",
      "reason":"manual smoke"}` → `approval.resolved` then
      `run.completed status=success` with the final summary; sequence
      numbers continued across the pause.

## 6. Batch (gate 8)

```bash
python - <<'EOF' > /tmp/batch.json
import json
print(json.dumps({
  "items": [{"item_id": f"item_{i:03d}", "input": {"name": f"caller {i}"}} for i in range(20)],
  "policy": {"max_parallel": 8, "max_cost_usd": "0.50"}
}))
EOF
curl -sN -X POST http://127.0.0.1:8080/batch \
  -H 'Content-Type: application/json' -d @/tmp/batch.json
```

- [ ] One SSE connection; every per-item frame carries `batch_id` +
      `item_id`; all 20 item_ids appear with `run.started` → terminal.
- [ ] Terminal `batch.completed` frame: total 20, succeeded 20, a
      real `total_cost_usd`.
- [ ] Budget enforcement: rerun with `"max_cost_usd": "0.0001",
      "max_parallel": 1` → after the first item, a
      `batch.budget_exceeded` frame, then 19 fast-fail
      `run.cancelled reason=batch_budget_exceeded` frames (no
      `run.started` for them), summary `budget_exceeded: true`.

## 7. Live-Redis shared rate limiter (gate 9)

Redis-py installed + Redis running (see Preconditions).

```bash
export FOUNDRY_RATE_LIMITER=redis://localhost:6379/0
export FOUNDRY_RATE_LIMIT_RPS=2 FOUNDRY_RATE_LIMIT_BURST=2
uv run python -m foundry serve projects/hello --port 8080 --workers 3
# hammer it from another terminal:
for i in $(seq 1 12); do
  (curl -s -o /dev/null -w '%{time_total}\n' -X POST \
    http://127.0.0.1:8080/run -H 'Content-Type: application/json' \
    -d '{"name": "load"}' &)
done; wait
```

- [ ] All 12 succeed, but wall time reflects the SHARED 2 rps bucket
      (~5–6s aggregate; without the limiter it's ~1s) — the three worker
      processes coordinate through Redis.
- [ ] `redis-cli keys 'foundry:rl:*'` shows the
      `anthropic:claude-haiku-4-5` token/last pair.
- [ ] Stop Redis mid-load → requests fail CLOSED with a structured
      `ProviderUnexpectedError` (`rate_limiter: unavailable`), not a
      hang or a stampede.
- [ ] Unset `FOUNDRY_RATE_LIMITER` → full speed returns (no gate).

## 8. Sustained load — the full recipe (gate 10)

The CI variant (20 concurrent submitters, ~10s, mock provider) runs in
`tests/integration/test_api_load.py`; raise it locally with
`FOUNDRY_LOAD_TEST_DURATION_S=30 uv run pytest tests/integration/test_api_load.py -q -s`.

The FULL docs/85 recipe (run when a real deployment is imminent; needs a
paid key budget or a stub provider):

1. Provision: 1 host, 4 workers, local Redis; point the provider at a
   stub (e.g. an nginx echo of a canned Anthropic response) unless you
   intend to pay for ~30k Haiku calls.
2. `FOUNDRY_RATE_LIMITER=redis://localhost:6379/0
   FOUNDRY_RATE_LIMIT_RPS=150 FOUNDRY_MAX_CONCURRENT_RUNS=100
   uv run python -m foundry serve projects/hello --port 8080 --workers 4`
3. Load: `hey -z 5m -q 100 -c 100 -m POST -T application/json
   -d '{"name": "load"}' http://127.0.0.1:8080/run`
   (or `oha`/`wrk2` at 100 rps for 5 minutes).
4. Assert afterwards:
   - [ ] `hey` reports 0 non-2xx (excluding deliberate 503 backpressure)
         and p95 latency within 2× of a single-run baseline.
   - [ ] 0 dropped events: sample ~100 run dirs under `~/.foundry/runs/`;
         `events.jsonl` sequences are contiguous, `run.started` first,
         terminal last (`python`-loop or `jq -s`).
   - [ ] 0 orphan pool connections: sampled `metadata.json`
         `connection_pool.acquires == connection_pool.releases`.
   - [ ] Over-budget runs (`max_cost_usd` variant of hello) fast-fail
         with `CostBudgetExceeded` in <1s each.
5. Graceful shutdown under load: `kill -TERM <uvicorn pid>` mid-load —
   the worker drains (503 + Retry-After for new runs), in-flight runs
   complete or cancel with `reason: worker_drain`, process exits 0.

## 9. Auth + prod guard

```bash
FOUNDRY_API_TOKENS=smoke-token uv run python -m foundry serve projects/hello --port 8082
curl -si -X POST http://127.0.0.1:8082/run -H 'Content-Type: application/json' -d '{"name":"x"}'
curl -si -X POST http://127.0.0.1:8082/run -H 'Authorization: Bearer smoke-token' \
  -H 'Content-Type: application/json' -d '{"name":"x"}'
curl -si http://127.0.0.1:8082/health
FOUNDRY_ENV=prod uv run python -m foundry serve projects/hello --port 8083
```

- [ ] Without the token: `401 {"error": "authentication required"}`;
      with it: 200; `/health` needs no token.
- [ ] `FOUNDRY_ENV=prod` WITHOUT `FOUNDRY_API_TOKENS` refuses to start
      (structured "NoAuth is forbidden" error, exit 2).

## Sign-off

- [ ] All boxes above checked.
- [ ] No secrets in any pasted output.
- [ ] Record the outcome + date in `docs/_demos/phase_8.md`.
