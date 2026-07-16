# Phase 10 demo — production Studio end-to-end (run for real, 2026-07-16)

The 10c hero demo has two halves: everything below was executed for real in
the implementation session (no Vite, no dev server, fake provider key); the
live-key half (streaming tokens, in-browser HITL approve, forge trajectory
with real commits) is the operator's manual pass —
`docs/_manual_tests/phase_10.md` §§ B/D.

## 1. Build the frontend, boot plain `foundry studio`

```console
$ cd ../agent-foundry-studio && npm run build
vite v8.1.4 building client environment for production...
✓ 2772 modules transformed.
dist/index.html                     0.98 kB │ gzip:   0.54 kB
dist/assets/index-CXDMcAnv.css     56.79 kB │ gzip:  10.24 kB
dist/assets/index-CQxm24CE.js   1,978.15 kB │ gzip: 621.28 kB
✓ built in 311ms

$ cd ../agent-foundry
$ ANTHROPIC_API_KEY=sk-fake-key-for-verification uv run foundry studio --port 4411 --no-open &

$ curl -s http://127.0.0.1:4411/api/health
{"status":"ok","version":"0.1.0","uptime_s":8.991,"active_forge_runs":0,
 "active_chat_sessions":0,"run_manager_pool":0}

$ curl -s -o /dev/null -w "%{http_code} %{content_type}\n" http://127.0.0.1:4411/
200 text/html; charset=utf-8
$ curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:4411/projects/team_hello/graph   # SPA deep link
200
$ curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:4411/assets/index-CQxm24CE.js
200
```

Production assets resolved from the sibling `../agent-foundry-studio/dist`
checkout — no `FOUNDRY_STUDIO_DIST`, no Node process at runtime.

## 2. Graph export — both fixture shapes

```console
$ curl -s http://127.0.0.1:4411/api/projects/team_hello/graph | jq -c '{pattern, primary_agent}'
{"pattern":"supervisor","primary_agent":"coordinator"}

nodes: (__start__ start) (coordinator agent/supervisor) (drafter agent/worker)
       (publisher agent/worker) (__end__ end)
edges: coordinator↔drafter handoff bidirectional=true
       coordinator↔publisher handoff bidirectional=true
       __start__→coordinator sequential · coordinator→__end__ sequential
```

`hello` returns the single shape (start → hello_agent → end) with the agent
card payload (`anthropic/claude-haiku-4-5`, prompt `v2`, tool pin
`catalog/http_get_json@v1`). Rendered in the browser at
`/projects/{hello,team_hello}/graph` — supervisor accent, doubled handoff
edges, side panel with pins + state scopes.

## 3. Chat — each message = one run; structured failure streams cleanly

With the deliberately fake key, the chat path exercises the full stack and
the failure contract in one move:

```console
$ curl -s -X POST http://127.0.0.1:4411/api/chat/hello/sessions -H 'Content-Type: application/json'
{"session_id":"s_01KXP2G0GVE6WCE90NJX3T3RV8", ...}

$ curl -s -X POST .../sessions/s_01KXP2G0.../messages -d '{"text":"hello there"}'
{"session_id":"s_01KXP2G0...","run_id":"01KXP2G0H93PR5JQZBXAVG0XFA", ...}

$ curl -sN .../sessions/s_01KXP2G0.../events
id: 0  event: run.started    {"run_id":"01KXP2G0H93PR5JQZBXAVG0XFA", ...}
id: 1  event: agent.started  {"agent_name":"hello_agent","agent_version":"v2", ...}
id: 2  event: llm.started    {"provider":"anthropic","model":"claude-haiku-4-5", ...}
id: 3  event: run.failed     {"error":{"error_class":"ProviderAuthError",
        "message":"anthropic rejected credentials (HTTP 401): invalid x-api-key",
        "context":{"http_status":401,"provider":"anthropic", ...}}}
```

In the browser the same frames render as a thread turn with the
`ProviderAuthError` chip and a "Retry message" button — the failure mode
table in docs/72 realised.

## 4. Widget layout persistence — server-side round trip

```console
$ curl -s -X PUT http://127.0.0.1:4411/api/layouts -d '{"version":1,"active":"default",
    "dashboards":{"default":{"widgets":[{"id":"w1","widget":"project-health",
    "config":{"project":"hello"},"layout":{"x":0,"y":0,"w":4,"h":3}}]}}}'
# → echoed back; and on disk:
$ head -3 ~/.foundry/studio/layouts.json
{
  "version": 1,
  "active": "default",
```

Reload + studio restart both read this file — the browser stores nothing.

## 5. Gates

```console
$ npx vitest run                 # frontend
Test Files  26 passed (26) · Tests  115 passed (115)
$ npx tsc --noEmit && npx eslint .          # clean
$ uv run python scripts/smoke_studio.py
68/68 checks passed · studio smoke: ALL GREEN
$ uv run ruff check src/ tests/ scripts/    # All checks passed!
$ uv run mypy --strict src/foundry/         # Success: no issues found in 235 source files
$ uv run pytest tests/ -q
1 failed, 1031 passed, 1 skipped        # the known 10a isolation bug: the
                                        # placeholder test requires the sibling
                                        # dist/ to be ABSENT (handoff § known issues)
```
