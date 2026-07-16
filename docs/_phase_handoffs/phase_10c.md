# Phase 10c handoff — Studio chat + flow graph + forge console + widget dashboards + packaging

**Date:** 2026-07-16 · **Frontend repo:** `../agent-foundry-studio`
(9 commits `6e4b380..97b12af+`; on top of 10b's 6) · **Backend repo:** docs-only
this phase (this commit). **Status:** implementation complete; awaiting the
AI review session + the operator's manual browser pass
(`docs/_manual_tests/phase_10.md`) — **v1.1.0 tags only after that pass.**

## What shipped (all frontend; backend untouched by design)

- **Chat** (`features/chat/`): per-project screen + `chat-panel` widget over
  the session SSE. Pure reducer (`chat-thread.ts`) folds multiplexed
  RunEvents into per-run turns (each message = one run): streamed
  `llm.delta` text, collapsible activity strip (tool calls w/ durations,
  node transitions, handoffs, warnings), in-thread approval cards
  (approve / reject-with-reason → `POST .../approvals`) collapsing to
  resolved badges, `run.completed(status=approval_pending)` treated as a
  pause, failed runs render the FoundryError envelope with retry,
  per-message run footer (run_id link/cost/tokens/latency) + session cost
  ticker, session list reattach (user text replayed from run artifacts via
  `userTextFromInputs`), single-turn vs multi-turn labelling from
  `ChatSessionInfo.multi_turn`, schema-input template offered when the
  message POST 422s with `required_fields`.
- **Flow graph** (`features/graph/`): `api/graph.ts` mirrors the normative
  GraphExport schema (hand-maintained: the route serialises via
  JSONResponse so the generated OpenAPI type is `unknown` — documented in
  the file header). Dagre auto-layout (LR/TB) in pure `graph-layout.ts`;
  custom node cards per kind/role (supervisor accent + crown, model/prompt/
  tool chips, dashed function cards, pill terminals); edge styling per kind
  (dashed conditional w/ predicate label, animated doubled bidirectional
  handoffs, thin joins); side panel with pins + state scopes + jump links;
  minimap/fit/zoom; `system_version` badge + recompile (query
  invalidation); 422 → ValidationResult panel linking into the config
  editor. `flow-graph-mini` renders the same component non-interactive.
- **Forge console** (`features/forge/`): launch form (structured params
  only) with 409 → "watch the active run"; `forge-trajectory.ts` merges
  historical `trajectory` records with live `forge.iteration_completed`
  frames (live wins per iteration); score chart vs threshold; commit-sha
  chips; `meta_agent.violation` → prominent destructive alert;
  `forge.terminated`/`forge.failed` → termination banner + stream close;
  cancel; history table + drill-in.
- **Widget dashboards** (`widgets/` + `dashboard/`): registry of exactly
  the settled 12; react-grid-layout v2 host (12 cols, rowHeight 80, drag
  handle on the frame title bar, min sizes from the registry); add-widget
  picker, per-widget config dialog (project/select/text fields), deep-link
  + remove chrome; named boards (tabs, create/delete/reset, last board
  protected); debounced (800 ms) `PUT /api/layouts` — server-side only;
  shipped defaults (default / forge board / chat board) used when the
  server document is empty; unknown widget ids → placeholder tiles.
  Pure helpers in `dashboard/dashboards.ts` (coerce/add/remove/apply/reset).
- **Approvals inbox** (`features/approvals/`): cross-project pending list;
  resolution via `POST /runs/{id}/resume` — the same path chat uses; mutual
  query invalidation keeps both surfaces consistent.
- **Run detail live view**: `EventFeed` attached to
  `GET /runs/{id}/events` (SSE replay + live follow); terminal frames close
  the client stream (no replay loop) and invalidate run queries.
- **Shell**: routes for `/`, chat, graph, `/forge(+id)`, `/approvals`; nav
  gains Dashboard/Forge/Approvals + per-project Chat/Flow graph.
- **CI**: `.github/workflows/ci.yml` — Node job (npm ci → lint → typecheck
  → test → build) + a `gen-check` job that checks out this repo, boots
  `foundry studio --dev`, and runs the drift check (needs
  `AGENT_FOUNDRY_TOKEN` if the backend repo is private). Note: the
  frontend repo has **no git remote yet**; the workflow is inert until one
  exists.
- **Docs (this repo)**: `docs/_manual_tests/phase_10.md` (full
  CLI-through-UI acceptance sheet, one row per CLI command),
  retro + demo, `docs/91` backlog updated (forge web UI delivered;
  Playwright stays v1.2), docs/90 tracking ticked.

## Verified against the real backend (this session)

`npm run build` then plain `uv run foundry studio` (production assets, no
Vite): `/api/health` 200; `/` serves the built index; deep link
`/projects/team_hello/graph` served by the SPA fallback; hashed assets 200.
`GET /api/projects/{hello,team_hello}/graph` return exactly the shapes the
frontend fixtures encode (single incl. tool pin; supervisor + 2 workers
with `bidirectional: true` handoffs). Chat round-trip with a **fake**
provider key: session opened, message POSTed, session SSE streamed
`run.started → agent.started → llm.started → run.failed` with the
structured `ProviderAuthError` envelope — rendered cleanly by the thread
(the same envelope the reducer tests assert). Layouts PUT → GET → 
`~/.foundry/studio/layouts.json` round-trip confirmed (file restored to an
empty document afterwards).

Live-key flows (real streaming tokens, HITL approve/reject round-trip in
the browser, forge trajectory with real commits) are covered by
`docs/_manual_tests/phase_10.md` §§ B/D — the 10a mock-transport seams are
test-internal (httpx.MockTransport in pytest), so a browser-level HITL
round-trip requires real keys; the approval UI logic is fully covered by
vitest (msw + EventSource mock, approve AND reject paths).

## Gates at close

- Frontend: vitest **115/115** (48 from 10b kept green + 67 new across
  chat/graph/forge/widgets/dashboard/approvals/run-events), `tsc --noEmit`
  clean, eslint clean, `npm run build` clean (one chunk-size warning —
  cosmetic, noted below).
- Backend (untouched, re-verified): ruff clean, `mypy --strict` clean
  (235 files), `scripts/smoke_studio.py` 68/68, pytest **1031 passed +
  1 skipped + 1 failed** — see known issue below (pre-existing 10a test,
  not a 10c regression; passes when the sibling `dist/` is absent).

## Known issues / for the review session

1. **Backend test-isolation bug (10a latent, surfaced now):**
   `tests/unit/test_studio_server.py::test_placeholder_page_serves_when_no_frontend_built`
   asserts the placeholder serves when "no frontend is built", but resolves
   assets via the real sibling-checkout convention — now that
   `../agent-foundry-studio/dist` exists (a required 10c outcome), the test
   finds real assets and fails. Verified: renaming `dist/` away → 9/9 pass.
   Fix belongs backend-side (monkeypatch the sibling path or point the
   resolver at a tmp dir); NOT fixed here per the phase fence (backend is
   read-only for the 10c session).
2. **Bundle size**: single 1.98 MB JS chunk (React Flow + CodeMirror +
   Recharts + RGL in one). Code-splitting by route is an easy v1.2 polish;
   irrelevant for a localhost tool.
3. **Packaging deliverable scope**: 10a already shipped the full asset
   resolution order (`FOUNDRY_STUDIO_DIST` → packaged `_assets/` → sibling
   dist → placeholder). The **release-build hook that copies `dist/` into
   the wheel** is still TODO and backend-side — flagged for the release
   session rather than done here (backend fence). Nothing else in the 10c
   gate depends on it.
4. Frontend repo still has no `origin` remote; CI workflow inert until the
   operator creates the GitHub repo (`gen-check` additionally needs
   `AGENT_FOUNDRY_TOKEN` for the private backend).

## Notable implementation decisions

- SSE consumption: EventSource has no wildcard listener, so `sse.ts`
  enumerates the studio event vocabulary (`STUDIO_EVENT_NAMES`) — a new
  backend event kind must be added there to reach the UI. Contract-worthy
  if it recurs.
- The chat thread derives everything from the event stream (single source
  of truth) — no client-side run bookkeeping beyond "text I sent this
  mount" (for optimistic user bubbles) + artifact replay for older runs.
- react-grid-layout v2 (the rewritten API: `gridConfig`/`dragConfig` +
  `useContainerWidth`) — `@types/react-grid-layout` is unused (v2 ships
  types) and can be dropped from devDependencies next housekeeping pass.
- jsdom test shims added for React Flow (DOMMatrixReadOnly, offsetWidth/
  Height, SVG getBBox) and a shared global `MockEventSource`
  (`tests/mock-event-source.ts`) — reuse it for any future SSE surface.

## What the operator does next

1. Fresh session: paste docs/90 § Phase 10c **review prompt**.
2. Run `docs/_manual_tests/phase_10.md` (both themes; keys for §§ B/D).
3. When green: `git tag v1.1.0 && git push --tags` (backend repo), and give
   the frontend repo a remote + first push.
