# Phase 10a — manual smoke test (operator checklist)

curl-driven walk of the studio control plane. Run each step by hand and
check the box. Prereqs: repo root checkout, `uv sync` done, `jq`
installed. No provider keys needed until the OPTIONAL live-LLM steps.

> The automated backbone is `uv run python scripts/smoke_studio.py`
> (68 route checks, mock provider). This checklist exercises the pieces
> that want a REAL server socket + human eyes.

## 1. Boot + placeholder + OpenAPI

```bash
cd <repo-root>
uv run foundry studio --no-open --port 8400
```

- [ ] Startup prints `[studio] no built frontend assets found — serving
      the placeholder page…` (frontend repo not built yet) and
      `control plane listening at http://127.0.0.1:8400/api`.
- [ ] `open http://127.0.0.1:8400/` shows the "Foundry Studio /
      frontend is not built yet" placeholder with working links.
- [ ] `curl -s localhost:8400/api/health | jq` → `status: "ok"`,
      version, uptime, pool counts.
- [ ] `curl -s localhost:8400/api/openapi.json | jq '.info.title'` →
      `"foundry studio"`.
- [ ] `curl -s localhost:8400/api/docs` renders Swagger UI in a browser.

## 2. Dev mode + auth refusals

```bash
uv run foundry studio --dev --port 8401
```

- [ ] Prints the Vite workflow pointing at the SIBLING repo:
      `cd ../agent-foundry-studio && npm run dev`.

```bash
uv run foundry studio --host 0.0.0.0 --port 8402
```

- [ ] Refuses to start (exit 2) naming `--auth-token` /
      `FOUNDRY_STUDIO_TOKEN`.

```bash
uv run foundry studio --host 127.0.0.1 --port 8403 --auth-token tok --no-open
curl -s -o /dev/null -w '%{http_code}\n' localhost:8403/api/health          # 401
curl -s -o /dev/null -w '%{http_code}\n' -H 'Authorization: Bearer tok' \
     localhost:8403/api/health                                              # 200
curl -s -o /dev/null -w '%{http_code}\n' 'localhost:8403/api/health?token=tok'  # 200 (SSE fallback)
```

- [ ] 401 / 200 / 200 as annotated.

## 3. Projects + validation round-trip (hero demo core)

Against the 8400 instance:

```bash
curl -s localhost:8400/api/projects | jq '.[].name'
curl -s localhost:8400/api/projects/hello/files | jq '.files[:5]'
curl -s -X POST localhost:8400/api/projects/hello/validate \
  -H 'Content-Type: application/json' \
  -d '{"path": "agents/hello_agent/agent.yaml", "content": "name: hello_agent\nstate_visibilty: {}\n"}' | jq
```

- [ ] Validate returns `ok: false` with an issue carrying `pointer`,
      `line`, `column`, and hint `did you mean "state_visibility"?` —
      the same text `foundry run` would print for that file.

## 4. Commit-on-save (USE A THROWAWAY BRANCH or revert after)

```bash
HASH=$(curl -s localhost:8400/api/projects/hello/files/agents/hello_agent/agent.yaml | jq -r .content_hash)
CONTENT=$(curl -s localhost:8400/api/projects/hello/files/agents/hello_agent/agent.yaml | jq -r .content)
jq -n --arg c "$CONTENT
# manual smoke edit" --arg h "$HASH" '{content: $c, base_hash: $h}' \
  | curl -s -X PUT localhost:8400/api/projects/hello/files/agents/hello_agent/agent.yaml \
      -H 'Content-Type: application/json' -d @- | jq
git log --oneline -1     # studio(hello): edit agents/hello_agent/agent.yaml
tail -1 projects/hello/.foundry/audit.jsonl | jq .operator   # {"kind": "studio", ...}
git revert --no-edit HEAD   # clean up
```

- [ ] Commit `studio(hello): edit …` appears; audit operator.kind is
      `studio`; revert leaves the tree clean.

## 5. Sandbox refusals

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X PUT \
  'localhost:8400/api/projects/hello/files/evals/greeting.yaml' \
  -H 'Content-Type: application/json' -d '{"content": "x: 1"}'          # 403
curl -s -o /dev/null -w '%{http_code}\n' -X PUT \
  'localhost:8400/api/projects/hello/files/..%2F..%2F..%2Fsrc%2Ffoundry%2Fevil.py' \
  -H 'Content-Type: application/json' -d '{"content": "x = 1"}'         # 403
```

- [ ] Both 403 with `error_class: SandboxViolation`; nothing written
      (`git status` clean; no `src/foundry/evil.py`).

## 6. Graph export

```bash
curl -s localhost:8400/api/projects/hello/graph | jq '{pattern, nodes: [.nodes[].id]}'
curl -s localhost:8400/api/projects/team_hello/graph \
  | jq '{pattern, handoffs: [.edges[] | select(.kind == "handoff")]}'
```

- [ ] hello: `single`, nodes `__start__ / hello_agent / __end__`.
- [ ] team_hello: `supervisor`; two bidirectional handoff edges
      coordinator↔drafter and coordinator↔publisher.

## 7. SSE with a real EventSource (browser)

With a paused/finished run available (e.g. after step 8), open the
browser console on the placeholder page:

```js
const es = new EventSource("/api/runs/<run_id>/events");
es.onmessage = (e) => console.log(e.lastEventId, e.data);
```

- [ ] Events replay in order; `lastEventId` increments; the stream
      closes at the terminal event.

## 8. OPTIONAL (real provider key): chat + forge live

With `ANTHROPIC_API_KEY` exported:

```bash
SID=$(curl -s -X POST localhost:8400/api/chat/hello/sessions | jq -r .session_id)
curl -s -X POST localhost:8400/api/chat/hello/sessions/$SID/messages \
  -H 'Content-Type: application/json' -d '{"text": "{\"name\": \"operator\"}"}' | jq
curl -N "localhost:8400/api/chat/hello/sessions/$SID/events"
```

- [ ] Tokens stream (`llm.delta` frames) and the run completes with a
      greeting.
- [ ] `foundry obs runs --since 1h` shows the chat-launched run
      (run_id threading: the same id appears in the studio logs with a
      `studio_request_id`).
- [ ] OPTIONAL forge: `curl -s -X POST localhost:8400/api/forge -H
      'Content-Type: application/json' -d '{"project": "<toy>",
      "description": "…", "eval_path": "projects/<toy>/evals/….yaml"}'`
      then `curl -N localhost:8400/api/forge/<id>/events` — iteration
      events with scores + commit shas stream live; a second launch for
      the same project while running returns 409.

## 9. Automated backbone

```bash
uv run python scripts/smoke_studio.py
```

- [ ] Ends `68/68 checks passed` / `studio smoke: ALL GREEN`.

## 10. Suite + gates

```bash
uv run ruff check src/ tests/ scripts/
uv run mypy --strict src/foundry/
uv run pytest tests/ -q
```

- [ ] All green; test count ≥ 999 baseline with the studio suites added.
