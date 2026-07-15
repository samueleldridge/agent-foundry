# Phase 10a retro

**What took longer than expected.** Two things. (1) SSE consumption in
tests: starlette's TestClient AND httpx's ASGITransport both buffer the
full response body, so the endless chat/forge streams hung every naive
test. The fix — a ~50-line ASGI-level collector (`studio_helpers.
stream_sse`) that drives the app coroutine and sends `http.disconnect`
once satisfied — is small, but finding the layer at which to fix it cost
a few probe cycles. (2) FastAPI ≥0.139 flattens `include_router` lazily
(`_IncludedRouter`), so enumerating `app.routes` for the parity contract
returned nothing; the factory now records `app.state.api_route_table` as
it assembles the routers, which is more honest anyway (the table IS the
assembly).

**What changed from the plan.** The operator moved the React frontend to
a separate repository (`agent-foundry-studio`, sibling checkout) mid-
phase. Backend impact was small — asset resolution became
`FOUNDRY_STUDIO_DIST` → packaged assets → sibling `dist/` → placeholder
— and docs/72 + the 10b/10c prompts were amended in a dedicated docs
commit. One behavioural change rode along: missing assets now serve a
placeholder page instead of exiting 2 (a backend-only checkout is a
first-class state when the frontend is elsewhere). Also: the chat pump
initially treated `run.completed(status=approval_pending)` as terminal
and the approval round-trip never resumed on the stream — the docs/32
pause-vs-terminal distinction bit exactly where the handoff for Phase 8
said it would.

**Cheap wins.** The Phase 6 `forge_helpers` scripted transport drove the
studio forge lifecycle tests unchanged; the Phase 8 hello/team
transports did the same for chat. Building `schemas.py` first and making
every route module a thin adapter kept mypy --strict trivially green.

**What 10b needs to watch.** (a) The frontend repo is greenfield — the
amended docs/90 prompt is authoritative over the stale "studio/ tree"
wording still in docs/03 § Phase 10b deliverables. (b) Generate types
from `/api/openapi.json`, not from schemas.py by hand. (c) The session
SSE `id:` is session-scoped (Last-Event-ID), with the run-scoped
sequence in `run_sequence` — the useSSE hook should resume on the frame
id. (d) `GET /api/forge/{id}/events` is live-process only; history comes
from `GET /api/forge/{id}` — the forge console should reconcile both.
