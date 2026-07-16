# 72 — Foundry Studio (web frontend)

## Purpose

Foundry Studio is a locally-served React webapp that puts **every foundry CLI feature behind a dedicated UI**: configuring new agents, editing existing ones, exploring tools and catalogs, running and comparing evals, monitoring cost and latency, running doctor, browsing versions and rolling back, driving the meta-agent (`foundry forge`), serving and inspecting runs — plus two capabilities the CLI cannot offer: a **chat frontend** for any Q&A-shaped project (streamed responses, in-chat HITL approvals) and a **flow-graph visualisation** of a project's compiled multi-agent topology.

`foundry studio` launches a control-plane FastAPI server + the built React assets on localhost; the browser is the whole surface. This is the v1.1 headline feature (it subsumes the "forge web UI" backlog item from `91-v1_1-backlog.md`).

Three load-bearing properties:

1. **Studio is a dev-time control plane, not a run-time serving layer.** It lives in a new top-level module `foundry.studio` that — like `foundry.cli` — may compose configurator + eval + versioning + api internals. The run-time serving layer (`foundry.api`) MUST NOT import it. `foundry serve` remains the production surface; Studio is the operator's workbench.
2. **The frontend never re-implements foundry semantics.** Flow compilation, config validation, versioning, pin resolution, cost math — all of it happens server-side through the same code paths the CLI uses. The frontend renders JSON the control plane produces. If the UI ever needs to "understand" a YAML file, that understanding is an API round-trip.
3. **Same safety rails as the meta-agent.** All config writes go through the config loaders for validation first, are committed via the versioning helpers, and are path-sandboxed to `projects/` via `foundry.security.PathSandbox`. Studio never writes `src/foundry/` or `catalog/` (read-only there), and no secret ever reaches the browser.

## Non-goals

- **Not a multi-user product.** Single operator, localhost-by-default, optional bearer token. No user accounts, no RBAC, no multi-tenancy (that posture lives in `86-multi-tenancy-and-ip.md` and doesn't change).
- **Not a replacement for `foundry serve`.** Production consumers keep hitting the auto-generated project API (`70-api-layer.md`). Studio's chat reuses that machinery in-process but is not a deployment target.
- **Not an ops dashboard replacement.** OTel backends remain the deep-observability surface; Studio renders the local SQLite mirror (`80-observability.md`).
- **Not a hosted/cloud UI.** No remote deployment story in v1.1. No telemetry leaves the machine.
- **No browser-driven E2E automation in v1.1.** Browser E2E is a manual checklist (`docs/_manual_tests/phase_10.md`); Playwright is a v1.2 candidate.
- **Not a mobile app.** Desktop-browser responsive (≥1024px primary; graceful down to tablet).

## Architecture

### Control plane vs run-time

```
                       browser (localhost:<port>)
                              │
              React SPA (the agent-foundry-studio repo, built by Vite)
                              │  fetch /api/*  ·  SSE  ·  static assets
                              ▼
   ┌───────────────────  foundry studio  ────────────────────┐
   │  foundry.studio (control-plane FastAPI)                 │
   │    ├── projects / configs / catalog / doctor / obs      │
   │    ├── evals / versions / rollback / storage / deploy   │
   │    ├── forge launcher (background task + SSE)           │
   │    ├── chat: per-project RunManager pool (in-process)   │
   │    ├── graph export: compile_project → FlowPlan → JSON  │
   │    └── layouts: widget-dashboard persistence            │
   │                                                          │
   │  imports (dev-time, like foundry.cli):                  │
   │    configurator · eval · versioning · api (internals)   │
   │    orchestration · observability · storage · security   │
   └──────────────────────────────────────────────────────────┘

   foundry.api (run-time)  ──✗──  MUST NOT import foundry.studio
```

`foundry.studio` sits beside `foundry.cli` and `foundry.configurator` at the top of the dependency diagram in `01-architecture-overview.md` § Module ownership. It is the fourth (and last) module allowed to compose eval + versioning + configurator, because it is the CLI's peer: a different transport for the same dev-time operations.

**New hard rule (extends the four in docs/01):**

5. **`api/` does not import `studio/`.** Same rationale as rule 4 (api ⊬ configurator): the API is run-time, the studio is dev-time. `foundry serve` must remain deployable without any studio (or Node-built) asset present.

Enforcement follows the established two-layer pattern:

- A nested ruff config at `src/foundry/api/ruff.toml` (extending the root, per the `src/foundry/core/ruff.toml` precedent — ruff replaces, not merges, `banned-api` tables, so the third-party bans are re-declared) adds:

  ```toml
  [lint.flake8-tidy-imports.banned-api]
  # ... third-party bans re-declared from root ...
  "foundry.studio".msg = "api must not import foundry.studio (run-time/dev-time boundary; see docs/01 rule 5)."
  "foundry.configurator".msg = "api must not import foundry.configurator (docs/01 rule 4; previously test-enforced only)."
  ```

- `tests/contract/test_import_boundaries.py` gains the same rule so a silent ruff-config loosening fails CI.

### As-built addendum to docs/01 § Directory layout

Rather than rewriting `01-architecture-overview.md`, this section records the additions to the target layout (docs/01 remains normative for everything else):

```
src/foundry/studio/        # NEW module — dev-time control plane (this doc)

../agent-foundry-studio/   # NEW SIBLING REPOSITORY — the React frontend
                           #   (its own git history, npm, package-lock.json;
                           #    this repo never contains node_modules or TSX)
```

**The frontend lives in a separate repository** (decided at Phase 10a): `agent-foundry-studio`, checked out as a sibling directory of this repo (e.g. `/Users/sam/projects/agent-foundry-studio`). Rules: framework developers edit it; the meta-agent never writes it; project operators never need to touch it; this repo carries **no** `studio/` tree, `node_modules`, or React/TS source. Built assets are produced by `npm run build` into the frontend repo's `dist/` and served by `foundry.studio.server` — located via `FOUNDRY_STUDIO_DIST` or the sibling-checkout convention (§ Packaging).

### Module layout — `src/foundry/studio/`

```
src/foundry/studio/
├── __init__.py            public surface: create_studio_app, StudioSettings
├── app.py                 control-plane FastAPI factory (mounts /api + static assets)
├── server.py              uvicorn runner behind `foundry studio`
├── schemas.py             Pydantic request/response models for every route (normative)
├── security.py            sandbox wiring (PathSandbox), redaction, optional bearer auth
├── events.py              StudioEvent envelope + SSE encoding (reuses foundry.api.streaming encoder)
├── projects.py            project discovery, summary, scaffold (project new)
├── configs.py             config file read / validate / write-with-commit
├── catalog.py             catalog browse / show / promote / deprecate
├── doctor.py              doctor checks as structured JSON
├── obs.py                 cost / latency / failures / eval-trend / runs queries (SQLite mirror)
├── runs.py                run history + RunArtifact readers + approvals inbox
├── evals.py               eval launch (background) + results + compare
├── versions.py            versions / diff / rollback / compute-version
├── forge.py               forge lifecycle: launch (background task), stream, cancel
├── chat.py                per-project in-process RunManager pool + chat sessions
├── graph.py               compile_project → FlowPlan → GraphExport JSON
├── connections.py         connections list / health / refresh / describe
├── storage.py             storage stats / gc / archive / pins
├── testing.py             `foundry test` launch + result surface
├── deploy.py              deploy dry-run / execute + compute-version
└── layouts.py             widget-dashboard layout persistence (~/.foundry/studio/layouts.json)
```

Each module owns one route group and delegates to the existing framework modules — `configs.py` calls `foundry.config` loaders and `foundry.versioning` commit helpers; `forge.py` drives `foundry.configurator.session`; `chat.py` instantiates `foundry.api.runs.RunManager` per project; `graph.py` calls `foundry.orchestration.compile_project`. **No business logic is duplicated in the studio layer**; it is adapters + schemas + task supervision.

### Frontend layout — the `agent-foundry-studio` repository

The frontend is its OWN repository (sibling checkout, e.g.
`/Users/sam/projects/agent-foundry-studio`), with its own git history and
npm tooling. Its internal layout:

```
agent-foundry-studio/      # separate repo root (sibling of this repo)
├── package.json           npm; package-lock.json committed; Node ≥ 26
├── vite.config.ts         React 19 + TS; dev proxy /api → 127.0.0.1:<port>
├── tsconfig.json          strict: true
├── eslint.config.js       typescript-eslint + react-hooks
├── index.html
├── src/
│   ├── main.tsx            entry; providers (QueryClient, Router, Theme)
│   ├── App.tsx             shell: sidebar nav, project switcher, theme toggle
│   ├── router.tsx          React Router route table (§ Routing map)
│   ├── api/                typed fetch client + TanStack Query hooks + SSE helpers
│   │   ├── client.ts       base fetch (bearer header, error envelope decode)
│   │   ├── sse.ts          EventSource wrapper with Last-Event-ID resume
│   │   └── hooks/          one file per route group (useProjects, useObsCost, ...)
│   ├── components/
│   │   ├── ui/             shadcn/ui primitives (Radix-based; generated, then owned)
│   │   ├── DataTable.tsx   TanStack-table wrapper; column defs per screen
│   │   ├── CodeEditor.tsx  CodeMirror 6 wrapper (YAML/markdown/python modes)
│   │   ├── EventFeed.tsx   virtualised RunEvent stream renderer
│   │   ├── DiffView.tsx    unified/side-by-side diff renderer
│   │   └── charts/         Recharts wrappers (CostChart, LatencyChart, TrendChart)
│   ├── features/           one folder per screen family
│   │   ├── projects/  configs/  catalog/  doctor/  obs/  runs/
│   │   ├── evals/  versions/  connections/  storage/  deploy/
│   │   ├── forge/  chat/  graph/  approvals/
│   ├── widgets/            widget registry + widget components (§ Widget system)
│   ├── dashboard/          react-grid-layout host, layout persistence hooks
│   ├── theme/              class-based dark/light provider + tokens
│   └── lib/                formatters (cost, tokens, durations), types generated from OpenAPI
├── tests/                  vitest + @testing-library/react
└── dist/                   build output (gitignored; produced by npm run build)
```

## The control-plane API surface

All routes live under `/api`. Everything is JSON except the SSE streams. Every response carries `X-Foundry-Studio-Version`; every error is a structured `FoundryError.to_dict()` envelope (per `70-api-layer.md` § Failure modes — never a stack trace). Every mutating route records to the project audit log (`52-rollback-and-audit.md`) with `operator: {kind: "studio"}`.

The parity rule this surface encodes: **every CLI feature in `foundry.cli.__main__` has a corresponding route**. The mapping is the Phase 10a exit gate's first item; the tables below are normative. Four commands map non-1:1, by design:

- `foundry run` → chat: each chat message starts a run through the same machinery (with a schema-driven input form for non-text projects), and the runs routes cover status/events/artifacts.
- `foundry serve` → the chat `RunManager` pool: Studio exercises a project's served behaviour in-process; launching a standalone production server stays a CLI act (Studio is not a deployment surface — § Non-goals).
- `foundry review` (TUI) → the versions/diff/rollback routes + screen (the TUI's web successor).
- `foundry studio` itself → `/api/health` (trivially).

### Projects

| Method | Path | Purpose | CLI parity |
|---|---|---|---|
| `GET` | `/api/projects` | List projects: name, branch, agent/tool counts, last commit, last eval score, health digest. `?include_bootstrap=true` also lists `foundry project new` skeletons (no system.yaml yet) with `bootstrap: true` — the forge console asks for these; run-shaped surfaces never see them | `foundry project list` |
| `GET` | `/api/projects/{name}` | Project detail: SystemSpec summary, agents, tools + pins, connections, flow pattern, guardrails | `foundry validate` (per-project view) + `GET /config` shape |
| `POST` | `/api/projects` | `{name, scaffold_eval=true}` → scaffold skeleton + `foundry/<name>` branch, PLUS (unless `scaffold_eval: false`) a validated starter eval template at `evals/<name>.yaml` — a minimal 3-case exact-scorer project-scope set with clearly-marked TODO placeholders, committed separately (`studio(<name>): scaffold starter eval template`). Forge requires an eval set; the template makes the new project forge-able immediately, and the operator fills the TODOs in before launching. Response carries `eval_path` (project-relative), `eval_repo_path` (what the forge form's `eval_path` wants) and `files`. Refusals are structured: a dirty working tree is a 400 whose `context.dirty_files` NAMES the uncommitted files; an existing project sets `context.exists` | `foundry project new` |
| `POST` | `/api/projects/{name}/test` | Launch project pytest; returns `{task_id}`; results via task polling | `foundry test` |

The starter eval is scaffolded **server-side by the project-new route**, not through `PUT /files/{path}`: the config-write path runs the meta-agent-shaped sandbox, which keeps `evals/` read-only (the eval is the target — docs/60). Scaffolding rides the same human-initiated action that created the project, and the template is validated by `load_eval_spec` before anything is committed. In the editor the file deep-links read-only; the operator edits the TODOs in their own tooling (or replaces the file) — the studio never opens an eval write path.

Config-editor file routes (`/files`, `/files/{path}`, `/validate`, `PUT /files/{path}`) accept bootstrap skeletons too, so the starter eval is browsable before the forge bootstrap writes system.yaml.

### Configs + validation

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/projects/{name}/files` | Config-file tree (YAML / prompts / output schemas / handlers), each tagged editable/read-only |
| `GET` | `/api/projects/{name}/files/{path}` | File content + kind (`system` / `agent` / `state` / `tool` / `prompt` / `python`) + schema ref |
| `POST` | `/api/projects/{name}/validate` | `{path, content}` → validate WITHOUT writing. Returns `ValidationResult` |
| `PUT` | `/api/projects/{name}/files/{path}` | Validate, then write + commit (`studio(<project>): edit <path>`). Refused on validation failure or sandbox violation |
| `GET` | `/api/schemas/{kind}` | JSON Schema for a config kind (feeds editor autocomplete; per `82-dev-ux.md` § Editor integration) |

`ValidationResult` (normative):

```python
class ValidationIssue(BaseModel):
    severity: Literal["error", "warning"]
    message: str                    # human-readable, same text the CLI prints
    pointer: str | None             # JSON pointer into the doc, e.g. "/agents/0/model_binding/provider"
    line: int | None                # 1-based line in the submitted content
    column: int | None
    hint: str | None                # Levenshtein "did you mean" etc. (docs/12)

class ValidationResult(BaseModel):
    ok: bool
    issues: list[ValidationIssue]
    kind: str                       # which schema validated it
```

The loader already produces file + pointer + line/column + received-vs-expected (docs/12); `configs.py` maps that structured error onto `ValidationIssue` — no re-parsing in the studio layer.

Worked round-trip:

```json
POST /api/projects/hello/validate
{
  "path": "agents/hello_agent/agent.yaml",
  "content": "name: hello_agent\nmodel_binding:\n  provider: anthropc\n  model: claude-opus-4-7\n..."
}

HTTP/1.1 200 OK
{
  "ok": false,
  "kind": "agent",
  "issues": [
    {
      "severity": "error",
      "message": "unknown provider 'anthropc'; available: anthropic, openai",
      "pointer": "/model_binding/provider",
      "line": 3,
      "column": 13,
      "hint": "did you mean 'anthropic'?"
    }
  ]
}
```

The same text the CLI prints for `foundry validate`, positioned for the editor gutter.

### Catalog

| Method | Path | CLI parity |
|---|---|---|
| `GET` | `/api/catalog?kind=tools\|connections\|retrievers` | `foundry catalog list` |
| `GET` | `/api/catalog/{kind}/{name}` | `foundry catalog show` — versions, `versions.json` metadata, eval scores, deprecations |
| `GET` | `/api/catalog/{kind}/{name}/{version}/files` | Read-only artifact source browse (5-file tool shape etc.) |
| `POST` | `/api/catalog/promote` | `{target, floor, strict_semver, allow_breaking, notes}` → human-gated promote; UI confirmation replaces the interactive prompt (`--yes` semantics with an explicit confirm step) |
| `POST` | `/api/catalog/deprecate` | `{ref, version, reason}` |

### Doctor

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/doctor` | Runs the full doctor check suite; returns `[{check, status: ok|warn|fail, detail, remedy}]` — same checks, same order as `foundry doctor --json` |

### Observability

All queries hit the local SQLite mirror via `foundry.observability.store` — never raw SQL in the studio layer.

| Method | Path | CLI parity |
|---|---|---|
| `GET` | `/api/obs/cost?project=&since=&by=model\|day\|agent` | `foundry obs cost` |
| `GET` | `/api/obs/latency?model=&project=&since=` | `foundry obs p95` (p50/p95/p99 series) |
| `GET` | `/api/obs/tool-failures?tool=&project=&since=` | `foundry obs tool-failures` |
| `GET` | `/api/obs/eval-trend?project=&since=` | `foundry obs eval-trend` |
| `GET` | `/api/obs/runs?project=&since=&status=` | `foundry obs runs` |
| `GET` | `/api/storage/stats` | `foundry storage stats` |
| `POST` | `/api/storage/gc` · `/api/storage/archive` | `foundry storage gc / archive` (dry-run first-class: `{dry_run: true}` default) |
| `GET/POST/DELETE` | `/api/storage/pins` | `foundry storage pin / unpin / list-pinned` |

### Runs + artifacts + approvals

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/runs?project=&status=&since=` | Run history (mirror + artifact store merged) |
| `GET` | `/api/runs/{run_id}` | Status + metadata (RunStatus shape from docs/70) |
| `GET` | `/api/runs/{run_id}/events?from_sequence=N` | SSE replay of persisted `RunEvent`s (reuses `foundry.api.streaming`) |
| `GET` | `/api/runs/{run_id}/artifact` | RunArtifact summary: inputs, outputs, state transitions, llm_calls / tool_calls indexes |
| `GET` | `/api/approvals?project=` | Pending approvals inbox | 
| `POST` | `/api/runs/{run_id}/resume` | `ApprovalResponse` (`--approve` / `--reject --reason`) — `foundry resume`, `foundry approvals approve/reject` |

### Evals

| Method | Path | CLI parity |
|---|---|---|
| `POST` | `/api/evals` | Launch: `{scope: project\|agent\|tool, target, eval_set, fail_under?}` → `{eval_run_id, task_id}`; progress streamed via `/api/tasks/{task_id}/events` | `foundry eval` / `eval tool` / `eval agent` |
| `GET` | `/api/evals?project=` | Recent eval runs | `foundry eval list` |
| `GET` | `/api/evals/{eval_run_id}` | Full per-case details | `foundry eval show` |
| `POST` | `/api/evals/compare` | `{tool, versions[]}` or `{project, pin_sets[]}` → `EvalComparison` | `foundry eval compare` |

### Versions / diff / rollback

| Method | Path | CLI parity |
|---|---|---|
| `GET` | `/api/projects/{name}/versions?tool=` | Commits + per-artifact version state | `foundry versions` |
| `GET` | `/api/projects/{name}/diff?ref1=&ref2=&path=` | Structured diff (per-file hunks) | `foundry diff` |
| `POST` | `/api/projects/{name}/rollback` | `{tool?\|prompt?\|to, force, dry_run}` — same pre-flight checks; `dry_run: true` is the default so the UI always previews first | `foundry rollback` |
| `GET` | `/api/projects/{name}/compute-version` | Content hash | `foundry compute-version` |

### Connections

| Method | Path | CLI parity |
|---|---|---|
| `GET` | `/api/projects/{name}/connections` | `foundry connections list` |
| `POST` | `/api/projects/{name}/connections/{conn}/health` | `foundry connections health` |
| `GET` | `/api/projects/{name}/connections/{conn}` | describe (redacted `ConnectionDescriptor`) |
| `POST` | `/api/projects/{name}/connections/{conn}/refresh` | force pool refresh |

### Forge lifecycle + streaming

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/forge` | Launch: `{project, description, eval_path, threshold, max_iter?, max_cost_usd, model, no_improvement_after}` → `{forge_run_id}`. Runs as a supervised background task in the studio server's lifespan task group. `max_iter` omitted/null resolves the global default: `FOUNDRY_FORGE_MAX_ITER`, else 5 (CLI parity with the optional `--max-iter`); `GET /api/health` reports the resolved default as `forge_max_iter_default` so the launch form can prefill it |
| `GET` | `/api/forge` | Forge runs (live + historical trajectories) — `foundry forge list` / `foundry obs forge` |
| `GET` | `/api/forge/{forge_run_id}` | Trajectory artifact: per-iteration scores, commits, artifacts touched — `foundry forge show` |
| `GET` | `/api/forge/{forge_run_id}/events` | **SSE**: live forge events — iteration started/completed, eval scores, per-iteration commit shas, meta-tool calls, sandbox violations, provider backoffs (`provider.retry`), termination (threshold / plateau / budget / max-iter / error) |
| `POST` | `/api/forge/{forge_run_id}/cancel` | Graceful cancel; trajectory artifact finalised as `cancelled` |

One forge run per project at a time (409 on concurrent launch for the same project). Sandbox violations and terminations are first-class event kinds so the UI can surface them loudly, not bury them in a log tail.

**Rate-limit backoffs are surfaced, not hidden.** When a provider 429s, the adapter's rate-limit schedule (docs/11 § Retry policy) waits it out and the runtime emits `provider.retry` (attempt, `delay_s`, `rate_limited`, `retry_after_s`) on the same stream; the forge console renders the live frame as an amber "Backing off Ns (rate limited) — attempt k" banner that clears on the next event. A rate-limited run reads as *slower*, never as hung or failed (docs/60 § Failure modes).

**New-project mode.** The forge console is also where NEW projects are born: a "New project" tab takes a name, calls `POST /api/projects` (skeleton + branch + starter eval template above), surfaces the created skeleton (branch, files), deep-links the starter eval into the config editor (`/projects/<name>/configs?file=evals/<name>.yaml`), and prefills the launch form with the new project + `eval_repo_path`. The dirty-working-tree refusal renders as actionable copy naming the uncommitted files from `context.dirty_files`.

### Chat

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/api/chat/{project}/sessions` | Open a chat session → `{session_id}`. Lazily boots an in-process `RunManager` for the compiled project (pool below) |
| `POST` | `/api/chat/{project}/sessions/{sid}/messages` | `{text}` → starts a run (**each chat message = one run**) → `{run_id}` |
| `GET` | `/api/chat/{project}/sessions/{sid}/events` | **SSE**: multiplexed `RunEvent` stream for the session's runs (`llm.delta`, `tool.started/completed`, `approval.required`, `run.completed`, ...) with Last-Event-ID resume |
| `POST` | `/api/chat/{project}/sessions/{sid}/approvals` | `ApprovalResponse` for an in-chat `approval.required` |
| `GET` | `/api/chat/{project}/sessions` | List sessions (for reattach after reload) |

Chat session SSE frames are ordinary `RunEvent`s in the docs/10 SSE envelope, prefixed with the owning run:

```
GET /api/chat/hello/sessions/s_01JX.../events
Content-Type: text/event-stream

id: 0
event: run.started
data: {"run_id":"01JXCHAT01","sequence":0,"event":"run.started",...}

id: 3
event: llm.delta
data: {"run_id":"01JXCHAT01","sequence":3,"event":"llm.delta","delta":{"type":"text","text":"Hel"}}

id: 9
event: approval.required
data: {"run_id":"01JXCHAT01","sequence":9,"event":"approval.required",
       "approval":{"approval_id":"send-email-...","prompt":"Send email to ...?","context":{...}}}

id: 10
event: approval.resolved
data: {"run_id":"01JXCHAT01","sequence":10,"event":"approval.resolved","decision":"approved",...}

id: 14
event: run.completed
data: {"run_id":"01JXCHAT01","sequence":14,"event":"run.completed","status":"success",...}
```

**RunManager pool**: `chat.py` maintains `{project → RunManager}` — the same `foundry.api.runs.RunManager` that `foundry serve` uses, bound into the studio app's lifespan task group with a SQLite checkpointer (so HITL pauses survive a studio restart). Compiled systems are cached and invalidated when a studio config save or rollback touches the project.

**Conversation carry**: where the project's state declares the `turns` read-scope convention (the v1 multi-turn mechanism, per `91-v1_1-backlog.md` § cross-session note), the session threads prior turns into each new run's input; otherwise each message is independent and the UI labels the chat "single-turn project". Studio does not invent a conversation mechanism the framework doesn't have.

### Graph export

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/projects/{name}/graph` | Compile the project (`compile_project` → `FlowPlan`) and return `GraphExport` JSON (§ Flow-graph visualisation). 422 with `ValidationResult` if the project doesn't compile |

### Deploy

| Method | Path | CLI parity |
|---|---|---|
| `POST` | `/api/projects/{name}/deploy` | `{image, target, platform, pre_deploy_eval?, production_floor, dry_run}` — `dry_run: true` default; the UI shows the platform command before applying | `foundry deploy` |

### Layouts, tasks, health

| Method | Path | Purpose |
|---|---|---|
| `GET/PUT` | `/api/layouts` | Widget-dashboard layouts document (§ Layout persistence) |
| `GET` | `/api/tasks/{task_id}` · `/api/tasks/{task_id}/events` | Generic background-task status + SSE progress (evals, tests, gc — anything long-running that isn't forge/chat) |
| `GET` | `/api/health` | Studio liveness: version, uptime, active forge runs, active chat sessions, RunManager pool size, `forge_max_iter_default` (the resolved `FOUNDRY_FORGE_MAX_ITER`, else 5 — prefills the forge launch form) |
| `GET` | `/api/openapi.json` | The control-plane OpenAPI schema (FastAPI-generated; feeds frontend type generation) |

## CLI: `foundry studio`

```
foundry studio [PROJECT_ROOT]
    --host 127.0.0.1        # bind address; non-loopback requires --auth-token or FOUNDRY_STUDIO_TOKEN
    --port 8400             # default studio port
    --dev                   # dev workflow: serve API only + print the Vite instructions
    --no-open               # don't auto-open the browser
    --auth-token <t>        # require Authorization: Bearer <t> on /api/*
```

Behaviour:

- **Production mode (default)**: serves `/api/*` plus the built SPA (resolved per § Packaging: `FOUNDRY_STUDIO_DIST` → packaged assets → the sibling `../agent-foundry-studio/dist` checkout) with an SPA history fallback (any non-`/api` 404 → `index.html`). Opens the browser at `http://127.0.0.1:8400`.
- **`--dev`**: serves the API only and prints `cd ../agent-foundry-studio && npm run dev` — Vite's dev server (port 5173) proxies `/api` to the studio port (configured in the frontend repo's `vite.config.ts`), giving HMR against the live control plane. `--dev` exists so the workflow is documented in `--help`, not just in this doc.
- Missing built assets without `--dev` → the control plane still boots and serves a "frontend not built" placeholder page naming the fix (build the `agent-foundry-studio` repo and/or set `FOUNDRY_STUDIO_DIST`). The frontend being a separate repo means a backend-only checkout is a first-class, fully working state.

## Frontend architecture

### Stack (settled)

React 19 + Vite + TypeScript (strict). **Tailwind CSS v4** + **shadcn/ui** (Radix primitives, generated into `src/components/ui/` and owned thereafter) + **lucide-react** icons. **TanStack Query** for all server state; **React Router** for navigation; **@xyflow/react** (React Flow) for the graph; **Recharts** for charts; **CodeMirror 6** for YAML/markdown/python editing; **react-grid-layout** for the widget dashboard. npm with committed `package-lock.json`; Node ≥ 26.

### Routing map

```
/                                  → Dashboard (widget grid; default layout)
/projects                          → Projects list
/projects/:name                    → Project overview (health card, pins, agents, recent runs)
/projects/:name/configs            → Config editor (file tree + CodeMirror + validation panel)
/projects/:name/graph              → Flow graph
/projects/:name/chat               → Chat
/projects/:name/evals              → Eval runs + launch + compare
/projects/:name/versions           → Commits, per-artifact versions, diff, rollback
/projects/:name/connections        → Connections + health
/projects/:name/runs               → Run history
/projects/:name/runs/:runId        → Run detail (event timeline, artifact, approvals)
/forge                             → Forge console (launch + live trajectory + history)
/forge/:forgeRunId                 → Forge run detail
/catalog                           → Catalog explorer (kind tabs → artifact → versions → files)
/approvals                         → Approvals inbox (cross-project)
/obs                               → Observability dashboards (cost / latency / failures / eval trend)
/doctor                            → Doctor panel
/storage                           → Storage stats + gc / archive / pins
/settings                          → Theme, auth token, layout reset
```

Screen inventory = one `features/` folder per top-level route. Deploy actions live inside the project overview (they're per-project, low-frequency).

### State management

- **Server state**: TanStack Query exclusively. Query keys mirror API paths (`["projects", name, "versions"]`). Mutations invalidate the narrowest affected keys (a config save invalidates that project's files, versions, and graph). No Redux; no server data copied into client stores.
- **Streams**: an `useSSE(url)` hook wraps `EventSource` with Last-Event-ID resume and exposes typed events; consumers (chat, forge console, run detail, event feed widgets) append to local component state, and terminal events (`run.completed`, forge termination) trigger query invalidation so tables/charts catch up.
- **Client state** (theme, sidebar, active dashboard tab): React context + `localStorage`. Widget layouts are server-persisted (§ Layout persistence) so they survive browser changes.
- **Types**: generated from `/api/openapi.json` via `openapi-typescript` at build time (`npm run gen:api`); the generated file is committed and drift is a CI check — the same "OpenAPI is real" property docs/70 establishes.

### Theming

Class-based dark/light (next-themes-style): a `data-theme`/`.dark` class on `<html>`, toggled from the shell header, persisted to `localStorage`, initial value from `prefers-color-scheme`, and an inline pre-hydration script to prevent flash. All colors are Tailwind v4 CSS-variable tokens (`--background`, `--foreground`, `--primary`, semantic status colors, and a chart series palette) defined once in `theme/tokens.css` for both modes — components and Recharts wrappers consume tokens, never hard-coded colors. Both modes are first-class: every screen must be checked in both (manual checklist item).

### Component-library conventions

- shadcn/ui components are **vendored, then owned** — edits allowed, wholesale re-generation discouraged. Anything used twice gets promoted from a feature folder to `src/components/`.
- Density: compact-professional. Tables over cards for data; cards for summaries. One accent color; status communicated by the semantic token set (ok/warn/fail) consistently across doctor, health, evals, and runs.
- Loading = skeletons (not spinners) for panels; inline spinners only for button-level actions. Errors render the structured `FoundryError` envelope (error_class + message + context), with a "copy details" affordance — mirroring the CLI's structured-error principle.
- Numbers: shared formatters in `lib/` for USD cost (4 decimal places under $1), token counts, durations, and relative timestamps — identical rendering everywhere.
- Accessibility: Radix gives keyboard/ARIA baselines; the checklist verifies focus order and contrast in both themes.

## The widget system

### Boundaries

A **widget** is a self-contained, data-fetching panel that renders inside the dashboard grid. Each widget = the summary form of a full screen, deep-linking into it. Widgets own their queries/streams (no dashboard-level data orchestration) so any combination composes.

Registry (v1.1 set — the settled 12):

| id | Widget | Renders | Config | Deep link |
|---|---|---|---|---|
| `project-health` | Project health card | validate + doctor digest, last eval score, branch, pin drift | project | `/projects/:name` |
| `runs-feed` | Runs feed | latest runs across (or per) project, live status | project?, limit | `/projects/:name/runs` |
| `cost-chart` | Cost chart | `obs/cost` series | project?, since, by | `/obs` |
| `latency-chart` | Latency chart | p50/p95 series | project?, model?, since | `/obs` |
| `eval-trend` | Eval trend | score-over-time + regression marker | project, since | `/projects/:name/evals` |
| `doctor-panel` | Doctor panel | check list with statuses; re-run button | — | `/doctor` |
| `forge-console` | Forge console | live trajectory of the active/most-recent forge run | project? | `/forge/:id` |
| `chat-panel` | Chat panel | full chat for one project | project (required) | `/projects/:name/chat` |
| `flow-graph-mini` | Flow graph mini | non-interactive fitted graph render | project (required) | `/projects/:name/graph` |
| `catalog-browser` | Catalog browser | compact kind/name/version list + search | kind? | `/catalog` |
| `approvals-inbox` | Approvals inbox | pending approvals with approve/reject inline | project? | `/approvals` |
| `versions-panel` | Versions / rollback | recent commits + one-click dry-run rollback | project | `/projects/:name/versions` |

Registry implementation: `widgets/registry.ts` maps `id → {component, title, icon, defaultSize, minSize, configSchema}`. Adding a widget = one registry entry + one component; the dashboard host is generic.

### Dashboard host + layout persistence

`react-grid-layout` grid (12 columns, responsive breakpoints). Add (from a registry picker), remove, drag, resize; per-widget config in a popover (project selector, time window). Multiple named dashboards (tabs), e.g. an "ops" board and a per-project "hello" board.

Persistence: `PUT /api/layouts` writes `~/.foundry/studio/layouts.json` (debounced on layout change) — server-side, not localStorage, so layouts survive browser resets and are trivially backupable:

```json
{
  "version": 1,
  "active": "default",
  "dashboards": {
    "default": {
      "widgets": [
        {"id": "w1", "widget": "project-health", "config": {"project": "hello"},
         "layout": {"x": 0, "y": 0, "w": 4, "h": 3}},
        {"id": "w2", "widget": "cost-chart", "config": {"since": "7d", "by": "day"},
         "layout": {"x": 4, "y": 0, "w": 8, "h": 3}}
      ]
    }
  }
}
```

Default layouts ship in the frontend (used when the file is absent or a dashboard is reset): **default** = project-health + runs-feed + cost-chart + eval-trend + approvals-inbox + doctor-panel; **forge board** = forge-console (large) + eval-trend + versions-panel; **chat board** = chat-panel (large) + flow-graph-mini. Unknown widget ids in a persisted layout render a placeholder tile (forward compatibility), never crash the board.

## Flow-graph visualisation

### `GraphExport` schema (normative)

Produced entirely server-side by walking the `FlowPlan` tree (`foundry.orchestration.patterns`); the frontend does layout + rendering only and **never re-implements flow semantics**.

```python
class GraphNode(BaseModel):
    id: str                          # unique node name (flow namespace)
    kind: Literal["agent", "function", "start", "end"]
    role: Literal["single", "supervisor", "worker", "step", "branch", "join"] | None
    label: str                       # display name
    group: str | None                # nested-subflow name (renders as a container)
    agent: AgentSummary | None       # for kind == "agent"
    function: FunctionSummary | None # for kind == "function"

class AgentSummary(BaseModel):
    model_binding: str               # "anthropic/claude-opus-4-7"
    prompt_version: str              # "v3"
    tools: list[str]                 # pinned refs, e.g. "catalog/word_stats@v2"
    state_read: list[str]
    state_write: list[str]

class FunctionSummary(BaseModel):
    version: str
    state_read: list[str]
    state_write: list[str]

class GraphEdge(BaseModel):
    id: str
    source: str
    target: str
    kind: Literal["sequential", "handoff", "conditional", "parallel", "join"]
    label: str | None                # predicate source for conditional; None otherwise
    bidirectional: bool = False      # supervisor ↔ worker handoff pairs collapse to one edge

class GraphExport(BaseModel):
    project: str
    system_version: str              # content hash — stale-graph detection after edits
    pattern: str                     # top-level flow type
    primary_agent: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    groups: list[str]                # nested-subflow container names
```

Pattern mapping (mirrors `FlowPlan` construction, including nesting): `single` → start → agent → end; `sequential` → chain of `sequential` edges; `parallel` → fan-out `parallel` edges from the predecessor, fan-in `join` edges into the join node; `supervisor` → supervisor node (`role: supervisor`) with `bidirectional` `handoff` edges to each worker; `graph` → declared edges, `conditional` where a predicate exists (edge label = predicate source text). Nested flows become `group` containers.

Worked example — `projects/team_hello/` (the Phase 7 supervisor + two workers example: coordinator drives drafter + publisher; publisher's tool is HITL-gated):

```json
GET /api/projects/team_hello/graph

{
  "project": "team_hello",
  "system_version": "9f21ac04be7d3311",
  "pattern": "supervisor",
  "primary_agent": "coordinator",
  "nodes": [
    {"id": "__start__", "kind": "start", "role": null, "label": "start", "group": null},
    {"id": "coordinator", "kind": "agent", "role": "supervisor", "label": "coordinator",
     "group": null,
     "agent": {"model_binding": "anthropic/claude-haiku-4-5", "prompt_version": "v1",
               "tools": [], "state_read": ["messages", "request"],
               "state_write": ["messages", "final_summary"]}},
    {"id": "drafter", "kind": "agent", "role": "worker", "label": "drafter", "group": null,
     "agent": {"model_binding": "anthropic/claude-haiku-4-5", "prompt_version": "v1",
               "tools": [], "state_read": ["messages", "request"],
               "state_write": ["messages", "draft"]}},
    {"id": "publisher", "kind": "agent", "role": "worker", "label": "publisher", "group": null,
     "agent": {"model_binding": "anthropic/claude-haiku-4-5", "prompt_version": "v1",
               "tools": ["local/publish_greeting@v1"],
               "state_read": ["messages", "draft"],
               "state_write": ["messages", "published"]}},
    {"id": "__end__", "kind": "end", "role": null, "label": "end", "group": null}
  ],
  "edges": [
    {"id": "e0", "source": "__start__", "target": "coordinator", "kind": "sequential",
     "label": null},
    {"id": "e1", "source": "coordinator", "target": "drafter", "kind": "handoff",
     "label": null, "bidirectional": true},
    {"id": "e2", "source": "coordinator", "target": "publisher", "kind": "handoff",
     "label": null, "bidirectional": true},
    {"id": "e3", "source": "coordinator", "target": "__end__", "kind": "sequential",
     "label": null}
  ],
  "groups": []
}
```

(State-field names are illustrative of the shape; the endpoint reads them from the compiled project.)

### Rendering

React Flow with a dagre auto-layout (left-to-right; top-down toggle). Custom node components per kind: agents show model + prompt version + tool count; functions show version; supervisor gets a distinct accent. Clicking a node opens a side panel with the full summary (tools with pins, state read/write) and jump links to the agent's config file and its runs. Edge styling by kind (solid sequential, dashed conditional with label, doubled handoff, thin grey join). Minimap + fit-view + zoom controls on the full screen; the `flow-graph-mini` widget renders the same graph non-interactive and fitted. The header shows `system_version` and a "recompile" button; a config save invalidates the graph query automatically.

## Chat UX (incl. HITL)

- **Layout**: standard message thread. Operator messages right-aligned; agent responses stream token-by-token from `llm.delta` events. A collapsible "activity" strip under each in-flight response shows tool calls (`tool.started/completed` with duration), node transitions in multi-agent projects, and retry/cache events — the observable run, not a black box.
- **Each message = a run**: the response footer shows run_id (link to `/runs/:runId`), cost, tokens, latency. A session cost ticker sums visible runs.
- **HITL**: on `approval.required`, an approval card renders **in the thread** at the pause point — prompt, tool name, arguments (redacted per the redaction rules), Approve / Reject-with-reason controls. Resolution posts the `ApprovalResponse`; the stream resumes in place; the card collapses to a resolved badge (decision + reason + operator time). Pending approvals also appear in the global approvals inbox; resolving from either surface updates both (query invalidation on `approval.resolved`).
- **Failures**: a failed run renders the structured error in-thread with a "retry message" affordance (new run, same input).
- **Reload behaviour**: sessions are listed server-side; reattaching replays the thread from persisted run artifacts and resubscribes live streams. HITL pauses survive studio restarts (SQLite checkpointer).
- **Suitability — schema-aware composer**: chat is offered for every project; the composer adapts to the project input model, which every `ChatSessionInfo` carries as `input_fields` (name / JSON-schema type / required, minus the auto-threaded `turns`). One required field → the plain message box, placeholder naming the field, text auto-wrapped server-side. Two-plus required fields → a compact per-field form (text inputs for strings; JSON-ish inputs for other types) that assembles the input object client-side, with an "edit as JSON" toggle for power users. The operator is never told to hand-write JSON — and raw-API callers who post non-JSON text to a multi-field project get a `ConfigValidationError` whose message and `context.template` carry a ready-to-fill JSON template of the required fields. This is what "chat frontend for any Q&A agent" degrades to for non-Q&A systems.

## Config-editing UX

- **Editor**: CodeMirror 6 — YAML mode for specs, markdown for prompts, python (read-only by default) for output schemas/handlers; toggle to allow python edits with a caution banner (they're code, and validation is import-based, not schema-based).
- **Validation round-trip**: on idle (debounced ~500ms) and on save, content posts to `/api/.../validate`; `ValidationIssue`s render as CodeMirror diagnostics (squiggles + gutter markers at `line`/`column`) plus a panel listing pointer + message + hint. **The frontend performs no schema validation of its own** — the loaders are the single validator, so studio errors are character-identical to CLI errors.
- **Commit-on-save**: save = validate → sandbox check → write → commit via the versioning helpers, message `studio(<project>): edit <path>` (conventional-commit style, `studio` type, project scope; commits carry no co-author lines, ever). The save response returns the commit sha, surfaced as a toast linking to `/projects/:name/versions`.
- **Refusals are visible**: validation failure → save disabled, errors inline; sandbox refusal (path outside `projects/<name>/`) → error naming the boundary; dirty-tree conflicts follow the rollback pre-flight rules.
- **Rollback affordance**: the versions screen (and widget) lists commits with per-artifact context; per-tool / per-prompt / per-project rollback runs dry-run first and shows the plan + pre-flight results before an explicit confirm — the trustworthy-rollback property (docs/52) with a UI on it. Prompt files get "new version" (scaffolds `v<N+1>.md` via the versioning helpers) and "pin version" actions matching the meta-tools' semantics.
- **Concurrent-edit safety**: `PUT` carries the content hash the editor loaded (`If-Match` semantics); a mismatch (file changed by forge or another session) → 409 with a diff, never a silent overwrite.

## Security posture

- **Localhost by default**: binds `127.0.0.1`. Binding non-loopback requires an explicit `--auth-token`/`FOUNDRY_STUDIO_TOKEN`; otherwise the server refuses to start (mirrors the `NoAuth`-refuses-prod rule in docs/70).
- **Bearer optional, then mandatory-if-exposed**: when a token is set, every `/api/*` route requires `Authorization: Bearer <token>`; SSE uses the token via query param fallback (EventSource can't set headers) with the value never logged.
- **PathSandbox**: every filesystem-touching route resolves paths through `foundry.security.PathSandbox` scoped exactly like the meta-agent's: writes only under `projects/`; `src/foundry/` and `catalog/` read-only (catalog gains write only through the promote route, which is the human-gated promotion path, not a file write). Traversal attempts (`..`, absolute paths, symlink escapes) are refused and logged as `studio.sandbox_refused` events.
- **No secrets to the browser — redaction rules**: (1) connection configs serve `ConnectionDescriptor.redacted_config` only; (2) run artifacts, events, and forge trajectories pass through the auth redactor before serialisation (same redactor as span attributes); (3) config file reads return raw text — safe because the secret-literal scan already guarantees configs contain env-var references, not credentials; the studio never resolves `${ENV:...}` for display; (4) `/api/doctor` reports secrets-provider reachability, never values; (5) a contract test plants a known fake credential in a fixture connection and asserts zero occurrences across every route's response body.
- **CSRF/CORS**: same-origin only (no CORS headers in production mode); the Vite dev proxy keeps dev same-origin too. Mutating routes reject `text/plain`-style simple requests by requiring `Content-Type: application/json`.
- **Command surface**: forge/eval/test/deploy launches accept structured params only — no shell strings from the browser, ever. Deploy defaults to dry-run.

## Observability of the studio itself

Studio operations join the audit trail like any execution path:

- Every control-plane request gets a `studio_request_id`; routes that launch framework work (runs, evals, forges, tests) log it alongside the spawned `run_id` / `eval_run_id` / `forge_run_id`, so "what did the studio trigger" is answerable from the standard `run_id`-threaded logs.
- Spans: `foundry.studio.request` (route, status, duration) parented above the framework spans the request spawns — a chat message's trace reads `studio.request → foundry.run → foundry.node → foundry.llm`.
- Structured events for studio-specific acts: `studio.config_saved` (path, commit sha), `studio.rollback` (mode, target), `studio.sandbox_refused`, `studio.forge_launched`, `studio.approval_resolved` — mirrored to the SQLite store so `foundry obs audit` shows studio actions beside forge/human ones.
- Mutations already covered by the versioning audit log record `operator: {kind: "studio"}` (extending the meta-agent-vs-human operator enum).
- No browser analytics; nothing leaves the machine. Frontend errors go to the browser console only.

## Testing strategy

Backend (same DoD as every phase):

- **Unit** (pytest): schema mapping for `ValidationResult` (loader error → pointer/line fidelity), graph export per pattern (single / sequential / parallel / supervisor / graph / nested), layout persistence round-trip, redaction of every route group's responses, sandbox refusals, forge/chat task supervision (launch, stream, cancel) with `MockProvider` — no LLM spend in tests.
- **Contract**: (a) **CLI-parity table test** — every CLI command in `foundry.cli.__main__` maps to a live route (the normative table in § API surface, encoded as data); a new CLI command without a route fails CI; (b) import-boundary: `foundry.api` ⊬ `foundry.studio` (ruff + `test_import_boundaries.py`); (c) credential-leak scan across all routes; (d) OpenAPI schema validates and covers every route.
- **API smoke script** (`scripts/smoke_studio.py`): boots `foundry studio` against the example projects, exercises **every route** (mock provider for run-shaped ones), asserts 2xx/expected-4xx + response-schema conformance. Runs in CI; is also the manual smoke test's backbone.
- `ruff` / `mypy --strict` / full pytest suite green; zero regressions to the v1.0.0 suite (998 tests).

Frontend:

- **vitest + @testing-library/react**: component tests for the load-bearing pieces — validation-diagnostic rendering, approval card flow, widget registry + dashboard add/remove/persist (layout PUT called with correct shape), graph rendering from fixture `GraphExport`s (hello + supervisor shapes), SSE hook resume logic. API via `msw` (mock service worker) with handlers generated from the OpenAPI types.
- **`tsc --noEmit`** and **eslint** clean; generated API types drift-checked in CI.
- **No browser E2E in v1.1**: the browser pass is the manual checklist `docs/_manual_tests/phase_10.md`, which walks every CLI-feature-through-UI claim in both themes. Playwright is the v1.2 candidate (recorded in `91-v1_1-backlog.md`).

CI additions: the Node job (`npm ci && npm run gen:api -- --check && npm run lint && npm run typecheck && npm test && npm run build`) lives in the `agent-foundry-studio` repo's own CI; in THIS repo the API smoke script gates merges touching `src/foundry/studio/`.

## Packaging

`npm run build` (in the `agent-foundry-studio` repo) → its `dist/`. Resolution order for `foundry.studio.server`: `FOUNDRY_STUDIO_DIST` env override (absolute path to a built `dist/`) → packaged assets under `foundry/studio/_assets/` (populated by the release build so `uv`-installed foundry ships a working studio without Node) → the sibling checkout `<repo_root>/../agent-foundry-studio/dist` (the dev convention). Neither present → the placeholder page. Node is a build-time dependency of the FRONTEND repo only; never a runtime requirement here, and this repo never contains `node_modules`.

## Invariants

1. **Every CLI feature is drivable through the studio** — enforced by the CLI-parity contract test.
2. **`foundry.api` never imports `foundry.studio`** — ruff + contract test; `foundry serve` works with no studio assets on disk.
3. **The frontend never re-implements foundry semantics** — validation, compilation, versioning, and cost math are server round-trips.
4. **Every studio write is validated-then-committed** — loaders before write, versioning-helper commit after, `studio(<project>): ...` message; no uncommitted config mutations.
5. **Studio writes are sandboxed to `projects/`** — `src/foundry/` and `catalog/` are read-only (promotion via the gated route only).
6. **No secret reaches the browser** — redactor on every response path; contract-tested with a planted credential.
7. **Chat and serve share one machinery** — `RunManager` in-process; no parallel chat runtime.
8. **Studio actions are auditable** — `studio_request_id` → `run_id` threading; audit-log entries with `operator.kind = "studio"`.
9. **Dark and light are both first-class** — token-based theming; both verified per screen.
10. **Node is never a runtime dependency** — built assets are served; only frontend development needs npm.

## Failure modes

| Cause | Surfaced |
|---|---|
| Built assets missing (no `--dev`) | boots anyway; placeholder page names the fix (`npm run build` in `agent-foundry-studio` / `FOUNDRY_STUDIO_DIST`) |
| Config save with invalid YAML | 422 + `ValidationResult`; editor shows inline diagnostics; nothing written |
| Write outside `projects/` | 403 `SandboxViolation`; `studio.sandbox_refused` logged |
| Project doesn't compile (graph/chat) | 422 with `ValidationResult`; UI links to the config editor at the failing file |
| Concurrent forge on same project | 409 with the active `forge_run_id`; UI offers "watch it instead" |
| Stale editor content on save | 409 + server/client diff; explicit re-load-and-merge |
| SSE disconnect | auto-reconnect with Last-Event-ID; replay from persisted events |
| Studio killed mid-forge / mid-chat-HITL | forge trajectory finalised as interrupted (resumable via `foundry forge --resume` semantics); chat approvals persist via checkpointer and reappear on restart |
| Non-loopback bind without token | refuses to start; actionable message |
| Provider credentials absent | chat/eval/forge launches fail with the structured provider error; doctor panel points at the missing env var |
| Project runtime secrets missing (connection `credentials_ref` env var unset, e.g. `COHERE_API_KEY`) | compile-dependent routes return **424** with a `ProjectUnavailableError` envelope (`context.env_vars` + `context.remedy`); the project detail exposes an `unavailable` block; the chat sessions list still returns stored sessions without compiling; the UI renders a `<ProjectUnavailableBanner>` (missing vars + remedy + link to the connections screen) with the composer disabled instead of an error wall. Compile semantics untouched — studio-surface handling only |

## Open questions

1. **Multi-repo support** — studio currently assumes one repo root (cwd, like the CLI). A root-switcher is plausible v1.2; defer.
2. **Prompt-diff visual mode** — side-by-side markdown rendering (not just text diff) when diffing prompt versions. Nice-to-have; defer unless cheap during 10c polish.
3. **Live run injection from the run-detail screen** (`InjectInput` mid-run) — the API supports it via resume; UI deferred until the next-run-injection semantics upgrade (see `91` § API/serving).
4. **Widget marketplace / user-authored widgets** — registry is code-level in v1.1; a plugin mechanism only if real demand.
5. **Notebook hand-off** — "open this run in a notebook" button emitting a snippet per `82` § Notebook ergonomics. Cheap; decide in 10c.
6. **Playwright E2E** — v1.2 candidate (tracked in `91`); the manual checklist is the v1.1 browser gate.
