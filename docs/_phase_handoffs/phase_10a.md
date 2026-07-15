# Phase 10a handoff — Studio control-plane API + `foundry studio` CLI

**Session date:** 2026-07-15
**Branch:** `main` (v1.0.0 baseline + Phase 10a commits)
**Status:** implementation complete; awaiting the AI review session + the
operator manual smoke test (docs/_manual_tests/phase_10a.md). Dev sandbox
had no provider keys — every LLM-shaped path verified via
httpx.MockTransport (chat via the Phase 8 hello/team transports; forge
via the Phase 6 scripted ForgeTransport). Git-touching tests run in
throwaway temp repos only.

## SCOPE CHANGE landed this phase (operator decision)

**The React frontend lives in a SEPARATE repository** —
`agent-foundry-studio`, checked out as a sibling of this repo (e.g.
`/Users/sam/projects/agent-foundry-studio`), with its own git + npm.
This repo never contains a `studio/` tree, `node_modules`, or TSX.
Consequences implemented here:

- Built assets resolve: `FOUNDRY_STUDIO_DIST` (absolute path to a built
  `dist/`) → packaged `foundry/studio/_assets/` (release wheels) →
  `<repo_root>/../agent-foundry-studio/dist` (sibling dev checkout) →
  the "frontend not built" placeholder page (`foundry studio` boots and
  serves the API regardless — a backend-only checkout is a first-class
  state; the docs/72 "exit 2 on missing assets" failure mode was
  replaced accordingly).
- docs/72 (§ architecture diagram, § as-built addendum, § frontend
  layout, § CLI, § testing/CI, § packaging, § failure modes) and the
  docs/90 Phase 10b/10c prompts were amended (commit `docs(studio)`).
  **Follow-up for 10b:** docs/03 § Phase 10b deliverable wording still
  says "studio/ tree" in two places; the 10b session should read the
  amended docs/90 prompt as authoritative.

## What this session built

### `foundry.studio` (new top-level module, ~20 files)

Per docs/72 § Module layout, plus one addition: `context.py`
(StudioContext/StudioSettings — repo layout, compiled-project cache,
registries, lifespan task-group handle; pure plumbing, no business
logic). Every route module is adapters + schemas over existing framework
modules; zero re-implementation of validation/compilation/versioning.

- `app.py` — `create_studio_app(repo_root, *, auth_token, checkpoint,
  transport, serve_assets)`. Mounts everything under `/api`
  (openapi at `/api/openapi.json`, docs at `/api/docs`), lifespan
  anyio task group (all chat runs / forge sessions / tasks are
  children), per-request `foundry.studio.request` span +
  `X-Studio-Request-Id` + `X-Foundry-Studio-Version` headers,
  structured FoundryError envelopes (SandboxViolation→403,
  `context.not_found`→404, RollbackError→409, else the docs/70 map),
  static/SPA serving with `/api` 404s kept JSON. Also records
  `app.state.api_route_table` — the as-assembled (method, path) set the
  contract tests enumerate (FastAPI ≥0.139 flattens include_router
  lazily, so walking `app.routes` no longer yields APIRoutes).
- `server.py` — `execute_studio(...)`: non-loopback bind without
  `--auth-token`/`FOUNDRY_STUDIO_TOKEN` refuses (exit 2); `--dev`
  serves API-only + prints the sibling-repo Vite workflow; placeholder
  notice when no frontend build resolves.
- `schemas.py` — the NORMATIVE request/response models 10b generates
  types from (ValidationIssue/ValidationResult, ProjectSummary/Detail,
  FileTree/FileContent/WriteRequest/WriteResult, catalog models,
  DoctorReport, ObsRows, storage models, RunListItem/RunArtifactView/
  ApprovalItem/ResumeRequest, eval models, VersionsResponse/DiffResponse/
  RollbackRequest+Response, ConnectionInfo/ConnectionHealthResponse,
  ForgeLaunchRequest/Response/ForgeRunInfo, ChatSessionInfo/
  ChatMessageRequest+Response, GraphExport/GraphNode/GraphEdge/
  AgentSummary/FunctionSummary, DeployRequest/Response, LayoutsDocument,
  TaskInfo/TaskLaunched, StudioHealth).
- `security.py` — bearer dependency (header + `?token=` SSE fallback,
  compare_digest), `redacted()` (the span-attribute redactor applied to
  response payloads), `studio_operator()` (audit Operator
  kind="studio").
- `events.py` — `StudioEvent` + `emit_studio_event()` (dispatched
  through the standard observability pipeline into the new
  `studio_events` mirror table), `EventLog` (sequence-numbered
  append/subscribe — the substrate for chat/forge/task SSE with
  Last-Event-ID resume), `sse_log_stream`, `resume_sequence`.
- Route modules: `projects` (list/detail/scaffold), `configs`
  (tree/read/validate/write-with-commit + `/api/schemas/{kind}`),
  `catalog` (list/show/files/promote/deprecate — both mutations
  confirm-gated), `doctor`, `obs` (store-backed cost/latency/
  tool-failures/eval-trend/runs), `storage` (stats/gc/archive/pins;
  gc+archive dry-run default), `runs` (history/status/SSE replay/
  artifact/approvals inbox/resume), `evals` (launch-as-task/list/show/
  compare), `versions` (versions/diff/rollback/compute-version),
  `connections` (list/describe/health/refresh), `forge`
  (launch/list/show/SSE/cancel), `chat` (sessions/messages/SSE/
  approvals), `graph` (FlowPlan→GraphExport), `deploy` (dry-run-default,
  as a task), `testing` (`foundry test` as a task), `layouts`
  (GET/PUT `<FOUNDRY_HOME>/studio/layouts.json`), `tasks` (status +
  progress SSE).

### Key mechanics 10b will consume

- **Validation**: `POST /api/projects/{name}/validate {path, content}`
  → `ValidationResult`. Shadow-copy of the project + the kind's real
  loader → `ConfigValidationError.context` maps 1:1 to
  pointer/line/column/hint; the shadow path is rewritten so `message`
  is character-identical to the CLI's error for the same content on
  disk (integration-tested against `load_agent_spec`). Prompt/markdown
  → empty-content warning; python → `compile()` syntax check.
- **Write path**: sandbox FIRST (403 + `studio.sandbox_refused` event,
  nothing validated/written) → validate (422 + issues, nothing
  written) → optional `base_hash` If-Match (409 + `StaleContent` with
  `server_content` for the merge UI) → write → `GitBackend.commit`
  (`studio(<project>): edit <path>`) → audit entry
  (`operator.kind="studio"`) → compiled-project + chat-pool
  invalidation.
- **Chat**: `ChatRegistry` = {project → RunManager pool} (the SAME
  RunManager `foundry serve` uses; sqlite checkpointer; bound to the
  app lifespan task group). Each message = one run. Session SSE frames
  are RunEvents re-stamped with a session-scoped `sequence` (`id:`)
  for Last-Event-ID; the run-scoped original rides as `run_sequence`.
  The pump treats `run.completed(status=approval_pending)` as a pause,
  not a terminal. Sessions are indexed on disk
  (`<FOUNDRY_HOME>/studio/chat_sessions.json`); after a restart the
  thread replays from run artifacts and pending approvals resume via
  `deliver_approval` (checkpointer path). `multi_turn` = the derived
  input model declares `turns` (memory_hello-style); prior turns thread
  into each new run's input; otherwise the session is labelled
  single-turn.
- **Forge**: `ForgeSupervisor` drives the real `ForgeSession` in the
  lifespan task group; the session's event_sink feeds the run's
  `EventLog` (SSE). Launch waits for `forge.started` (which carries the
  session-minted forge_run_id) or a pre-flight error. One-per-project:
  409 + active forge_run_id. Cancel = CancelScope cancel; the
  supervisor finalises `meta.json` as
  `termination_reason="user_cancelled"` over whatever trajectory
  reached disk and emits a terminal `forge.terminated` frame. List/show
  merge live tasks with `~/.foundry/runs/*/meta.json` artifacts
  (discriminated by the `forge_run_id` key).
- **Graph export**: `_GraphBuilder` walks the FlowPlan union —
  single/sequential/parallel(+join/then)/supervisor(bidirectional
  handoff edges, workers_may_end → worker→__end__)/graph(conditional
  labels = `CompiledPredicate.source`); nested subflows become `group`
  containers. AgentSummary reads model_binding/prompt pin/tool pins
  from the spec + state read/write from the compiled state views.
  Non-compiling project → 422 with ValidationResult.
  NOTE: `system_version` here is `CompiledProject.system_version`
  (compiler definition), while `/compute-version` returns the docs/84
  content hash — both stable per commit; 10b should treat them as
  opaque staleness tokens, not compare across endpoints.
- **Tasks**: evals / project tests / deploys run via `TaskRegistry`
  (`{task_id}` + `GET /api/tasks/{id}` + `/events` SSE, terminal
  `task.completed|task.failed`). Eval launch returns 202 with task_id;
  the eval_run_id arrives in the task result (it is minted inside
  run_eval — the docs/72 sketch's `{eval_run_id, task_id}` is
  approximated as `{task_id}` + result).

### Framework-side touches (small, additive)

- `observability/store.py`: new `studio_events` table (additive DDL,
  schema_version stays 1) + `record_event` branch for `studio.*` +
  `studio_events()` query.
- `versioning/audit.py`: `Operator.kind` literal gains `"studio"`.
- `cli/__main__.py`: the `foundry studio` command.
- `src/foundry/api/ruff.toml`: NEW nested config banning
  `foundry.studio` + `foundry.configurator` inside api/ (third-party
  bans re-declared per the replace-not-merge rule).

## Testing / mock patterns used

- Chat: `httpx.MockTransport` hello reflector + the team_hello
  per-agent marker transport (replicated in
  `tests/integration/studio_helpers.py`).
- Forge: `tests/integration/forge_helpers.py` (Phase 6) —
  `ForgeTransport` scripted meta turns + computed project turns; one
  iteration to threshold_met. Cancel tests hang the meta turn on an
  asyncio.Event.
- **SSE consumption**: starlette TestClient AND httpx ASGITransport
  buffer full bodies, so endless session/forge streams can't be read
  through them. `studio_helpers.stream_sse()` drives the ASGI app
  coroutine directly, parses frames per chunk, and sends
  `http.disconnect` when the stop condition fires (= a browser closing
  its EventSource). Reuse it in 10b/10c backend tests.
- Contract: `test_studio_parity.py` (typer-app walk → PARITY table →
  route existence via `app.state.api_route_table`; OpenAPI covers every
  route), `test_studio_redaction.py` (planted
  `HELLO_SERVICE_API_KEY` value; zero hits across ~30 route bodies +
  chat SSE + artifacts), `test_import_boundaries.py` extended (api ⊬
  studio/configurator scan + ruff-fires probe).
- `scripts/smoke_studio.py`: 68 checks over every route group against
  a throwaway repo (hello + team_hello), incl. the chat approval
  round-trip and eval/deploy task polling. Exit 0 = green.

## Env vars added

- `FOUNDRY_STUDIO_TOKEN` — bearer token (required for non-loopback).
- `FOUNDRY_STUDIO_DIST` — absolute path to the frontend repo's built
  `dist/`.

## Deviations from docs/72 (deliberate; review notes)

1. **Frontend-in-separate-repo** — operator scope change (see top).
2. `context.py` added to the module layout (plumbing only).
3. Missing assets ≠ exit 2 — placeholder page (scope change).
4. `POST /api/evals` returns `{task_id}` (202); eval_run_id in the task
   result (minted inside the harness).
5. Doctor rows: `{check, status, detail, remedy: null}` — v1 doctor has
   no separate remedy field; remedies live in `detail` text.
6. Catalog deprecate is implemented as the second confirm-gated catalog
   mutation (docs/03 wording says promote is "the only catalog
   mutation"; docs/72 § API surface lists deprecate — the route
   mutates versions.json metadata only, committed
   `studio(catalog): deprecate …`; no audit-log file under catalog/ to
   keep the shared tree clean).
7. Chat session SSE stays open across runs (per spec); the `id:` is the
   session sequence, with `run_sequence` preserved in the payload.
8. `GET /api/forge/{id}/events` requires a live (this-process) forge —
   historical trajectories come from `GET /api/forge/{id}` (no
   events.jsonl replay for forge in v1; the session writes
   trajectory.jsonl, not per-event artifacts suitable for SSE replay).
9. Storage archive dry-run previews via the gc candidate scan (the
   retention module's `archive()` has no dry-run parameter).
10. `foundry obs forge` CLI does not exist (v1.1 deferral, Phase 9
    deviation 2 stands); `GET /api/forge` reads run-dir artifacts
    directly rather than a mirror table.

## DoD confirmation

- `uv run ruff check src/ tests/ scripts/` — zero violations (incl. the
  new nested api config; probe tests prove both nested configs fire).
- `uv run mypy --strict src/foundry/` — clean (235 files).
- `uv run pytest tests/` — full suite green: **1032 passed + 1 skipped
  (1033 collected)**; the 999-test v1.0.0 baseline intact, +34 studio
  tests (15 integration, 13 unit, 6 contract).
- `scripts/smoke_studio.py` — 68/68.
- run_id threading: chat/forge/eval launches log
  `studio_request_id` beside the spawned run ids;
  `foundry.studio.request` spans wrap every request; `studio.*` events
  mirror to the store.
- No secrets in code/configs/fixtures; the studio credential-leak
  contract test enforces the browser-facing surface continuously.

## What Phase 10b consumes first

1. `docs/90` § Phase 10b prompt (amended for the separate repo).
2. This handoff's § Key mechanics + `schemas.py` (normative shapes).
3. `GET /api/openapi.json` from `foundry studio --dev` for type
   generation.
4. `studio_helpers.stream_sse` if backend-side SSE tests are needed.
