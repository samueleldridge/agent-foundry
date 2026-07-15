# Phase 10a demo — the studio control plane, curl-walked

Hero command (mock provider; throwaway repo; ~5 s wall time):

```bash
uv run python scripts/smoke_studio.py
```

Recorded output (trimmed; full run is 68 checks):

```
== health / meta ==
  [ok  ] GET /api/health
  [ok  ] GET /api/openapi.json
  [ok  ] GET / (placeholder or SPA)
== projects + configs ==
  [ok  ] GET /api/projects
  [ok  ] POST /api/projects/hello/validate
  [ok  ] validate reports structured issues
  [ok  ] PUT config commits studio(hello): edit ...
  [ok  ] sandbox refuses evals/ write (403)
== catalog / doctor / obs / storage ==
  [ok  ] GET /api/catalog … GET /api/doctor … obs × 5 … storage × 6
  [ok  ] promote is confirm-gated
== chat / runs / approvals ==
  [ok  ] POST /api/chat/hello/sessions
  [ok  ] chat SSE streams llm.delta → run.completed
  [ok  ] GET /api/runs/01KXJBTBVHFV5PBVSYQM252YFV/artifact
== team chat approval round-trip ==
  [ok  ] POST .../approvals
  [ok  ] approval round-trip resumes to run.completed
== evals / versions / graph / connections / deploy ==
  [ok  ] project eval task completes
  [ok  ] rollback defaults to dry-run
  [ok  ] GET /api/projects/hello/graph
  [ok  ] GET /api/projects/team_hello/graph
  [ok  ] deploy dry-run task completes
== layouts ==
  [ok  ] PUT /api/layouts

68/68 checks passed
studio smoke: ALL GREEN
```

The forge slice (launch → live SSE trajectory with per-iteration score +
commit shas → threshold_met; 409 on concurrent launch; cancel finalises
the artifact as `user_cancelled`) is exercised by
`tests/integration/test_studio_forge.py` against the Phase 6 scripted
ForgeTransport (a real meta-agent run needs a provider key — manual
checklist § 8).

`foundry studio` itself boots the same app under uvicorn:

```
$ uv run foundry studio --no-open
[studio] no built frontend assets found — serving the placeholder page.
Build them (Phase 10b+) in the agent-foundry-studio repo (`npm run
build`) and point FOUNDRY_STUDIO_DIST at its dist/ (a sibling checkout
is found automatically).
[studio] control plane listening at http://127.0.0.1:8400/api
INFO:     Uvicorn running on http://127.0.0.1:8400 (Press CTRL+C to quit)
```
