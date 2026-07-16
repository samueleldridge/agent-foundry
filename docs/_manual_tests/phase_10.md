# Phase 10 manual browser checklist — every CLI feature through the Studio UI

This is the operator's acceptance sheet for v1.1 (Phases 10a–10c together).
**v1.1.0 tags only after this pass is green.**

Prereqs:

```bash
cd ../agent-foundry-studio && npm install && npm run build
cd ../agent-foundry && uv run foundry studio          # http://127.0.0.1:8400 (or --port)
```

- Run the whole sheet **twice: once in dark, once in light** (toggle in the
  sidebar header; the choice must persist across reloads).
- Steps marked **[keys]** invoke an LLM and need a real `ANTHROPIC_API_KEY`
  (or a second provider for the provider-swap row). Everything else runs
  against local state. For a no-keys pass of chat error handling, export a
  fake key and expect the structured `ProviderAuthError` to stream into the
  thread (verified working at implementation time).
- No Vite process anywhere: this sheet exercises the **production build**
  served by `foundry studio` itself, including deep links (paste
  `/projects/team_hello/graph` directly into the address bar → the SPA
  history fallback must serve the app, not a 404).

## A. CLI-parity rows — one row per CLI command

| # | CLI command | UI path | Verify |
|---|---|---|---|
| A1 | `foundry run` | project → **Chat** (`/projects/hello/chat`) | Each sent message becomes one run; footer run_id links to `/projects/hello/runs/<id>` **[keys]** |
| A2 | `foundry resume` | **Approvals** (`/approvals`) or the in-chat approval card | Approve and reject-with-reason both resume the paused run **[keys]** |
| A3 | `foundry approvals list` | **Approvals** (`/approvals`) | Pending HITL approvals across projects, with redacted tool context |
| A4 | `foundry connections health` | project → **Connections** | List + describe (redacted) + health check + refresh buttons work |
| A5 | `foundry forge` | **Forge** (`/forge`) | Launch form (description/eval/threshold/max-iter/cost cap/model); live trajectory on `/forge/<id>` **[keys]** |
| A6 | `foundry project new` | **Projects** → "New project" | Scaffold created + `foundry/<name>` branch |
| A7 | `foundry catalog list` | **Catalog** (`/catalog`) | Tools/connections/retrievers tabs with versions |
| A8 | `foundry catalog show` | **Catalog** → artifact | versions.json metadata, eval scores, deprecations, file browse |
| A9 | `foundry catalog promote` | **Catalog** → artifact → "Promote" | Human-gated confirm step; commit visible in git |
| A10 | `foundry eval` | project → **Evals** | Launch (scope/eval set/fail-under), per-case results, history, compare **[keys]** |
| A11 | `foundry rollback` | project → **Versions** | Dry-run plan + pre-flight first, explicit confirm to apply |
| A12 | `foundry versions` | project → **Versions** | Commits + per-artifact version state |
| A13 | `foundry diff` | project → **Versions** → select two refs | Per-file hunks render |
| A14 | `foundry serve` | project → **Chat** (in-process RunManager pool) | Chat exercises the same serving machinery; sessions listed for reattach |
| A15 | `foundry obs cost` | **Observability** (`/obs`) | Cost chart matches `foundry obs cost --json` |
| A16 | `foundry obs p95` | **Observability** | Latency percentiles chart |
| A17 | `foundry obs tool-failures` | **Observability** | Per-tool failure table |
| A18 | `foundry obs runs` | **Observability** / project → **Runs** | Run history rows match the mirror |
| A19 | `foundry obs eval-trend` | **Observability** / project → **Evals** | Score-over-time chart |
| A20 | `foundry storage stats` | **Storage** (`/storage`) | Usage by kind |
| A21 | `foundry storage gc` | **Storage** → GC | Dry-run default shows candidates; apply deletes |
| A22 | `foundry storage archive` | **Storage** → Archive | Dry-run preview then apply |
| A23 | `foundry storage pin` | **Storage** → Pins | Pin an artifact |
| A24 | `foundry storage unpin` | **Storage** → Pins | Unpin it again |
| A25 | `foundry storage list-pinned` | **Storage** → Pins | Pinned list renders |
| A26 | `foundry test` | project overview → "Run tests" | Task launches; progress + results stream via the task SSE |
| A27 | `foundry doctor` | **Doctor** (`/doctor`) | Same checks, same order as `foundry doctor --json`; re-run button |
| A28 | `foundry review` | project → **Versions** (the TUI's web successor) | Commits + evals + diff + rollback from one screen |
| A29 | `foundry compute-version` | project overview | Content hash displayed and matches the CLI |
| A30 | `foundry deploy` | project overview → Deploy | Dry-run default shows the platform command before applying |
| A31 | `foundry studio` | the app itself + `/api/health` | Health payload shows version/uptime/pools |
| A32 | *(config editing — meta-tool/CLI-adjacent)* | project → **Configs** | Break a YAML → inline CLI-identical diagnostics, save blocked; fix → save → `studio(<project>): edit <path>` commit |

## B. Chat + HITL (docs/03 § 10c gate items 1–2) **[keys]**

- [ ] B1 `hello`: send a message → tokens stream live (`llm.delta`); activity strip shows the `get_time` tool call with duration; footer shows run_id/cost/tokens/latency; session cost ticker sums up.
- [ ] B2 `team_hello`: ask it to draft + publish → an approval card renders **in the thread** at the pause point with prompt + redacted args.
- [ ] B3 Approve → the run resumes in place; the card collapses to an "Approved" badge that persists in the thread.
- [ ] B4 Repeat with **Reject** + a reason → run resumes down the reject path; badge shows the reason.
- [ ] B5 Browser reload mid-session → the thread replays from artifacts and the live stream reattaches.
- [ ] B6 Studio **restart** with an approval pending → the pause survives (checkpointer); the approval reappears in chat AND in `/approvals`; resolving in the inbox updates the open chat (and vice versa).
- [ ] B7 Failed run (e.g. fake key): the structured error (`ProviderAuthError` envelope) renders in-thread with a working "Retry message".
- [ ] B8 Single-turn vs multi-turn labelling: `hello` is labelled single-turn; `memory_hello` threads prior turns.

## C. Flow graph (gate item 3)

- [ ] C1 `/projects/hello/graph`: start → hello_agent → end; agent card shows `anthropic/claude-haiku-4-5`, `prompt v2`, `1 tool`.
- [ ] C2 `/projects/team_hello/graph`: coordinator (supervisor accent + crown) with **bidirectional** handoff edges to drafter + publisher; sequential edges from start and to end.
- [ ] C3 Node click → side panel: model, prompt version, tools with pins, state read/write; "Open config" and "View runs" links land correctly.
- [ ] C4 Minimap, fit, zoom, LR↔TB toggle all work; header shows `system_version`; edit a config → Recompile updates the hash.
- [ ] C5 Break the project's YAML → graph screen shows the ValidationResult with a link into the config editor (fix it afterwards).

## D. Forge console (gate item 4) **[keys]**

- [ ] D1 Launch a forge run against `hello` + its eval set from the form.
- [ ] D2 Live trajectory: per-iteration scores charted against the threshold; per-iteration commit shas listed (cross-check `git log`).
- [ ] D3 Prompt an out-of-sandbox write (or replay a run that had one) → the sandbox violation renders as a prominent alert, not a log line.
- [ ] D4 Termination reason surfaced (threshold/plateau/budget/max-iter); history row + drill-in match `~/.foundry/runs/*/meta.json`.
- [ ] D5 Cancel from the UI mid-run → trajectory finalised as cancelled.
- [ ] D6 Concurrent launch for the same project → 409 with "Watch the active run instead".

## E. Widget dashboards (gate items 5–6)

- [ ] E1 `/` shows the shipped **default** board (project-health, runs feed, cost, eval trend, approvals, doctor) with live data; **forge board** and **chat board** tabs exist.
- [ ] E2 Add a widget from the picker; remove one; drag one; resize one; set per-widget config (e.g. cost-chart → 30d) — all stick.
- [ ] E3 Reload the browser → board intact (served from `~/.foundry/studio/layouts.json`, not localStorage).
- [ ] E4 Restart `foundry studio` → board still intact.
- [ ] E5 All 12 registry widgets render with live data and deep-link to their full screens (add each once: project-health, runs-feed, cost-chart, latency-chart, eval-trend, doctor-panel, forge-console, chat-panel, flow-graph-mini, catalog-browser, approvals-inbox, versions-panel).
- [ ] E6 Hand-edit `layouts.json` to add a widget id that doesn't exist → placeholder tile ("Unknown widget"), never a crash; Reset board restores the shipped default.
- [ ] E7 Create a new named dashboard; delete it; the last remaining board cannot be deleted.

## F. Production serving + polish (gate items 8, plus 10b regressions)

- [ ] F1 Kill any Vite process. Every screen functional on the `foundry studio` port alone; hashed assets load; deep links (paste any route) serve the SPA.
- [ ] F2 Placeholder page: temporarily rename `../agent-foundry-studio/dist` → `foundry studio` boots and serves the "frontend not built" page naming the build command; restore afterwards.
- [ ] F3 No browser-console errors on any screen visited in this sheet.
- [ ] F4 Keyboard pass: tab order sane on chat composer, approval cards, dashboard picker dialogs; Escape closes dialogs.
- [ ] F5 1024px window: no horizontal scrolling; sidebar + content usable.
- [ ] F6 Both themes verified (this sheet run twice).
- [ ] F7 SSE resilience: kill and restart the studio while a chat stream is open → the EventSource reconnects with Last-Event-ID and replays.

## G. Cross-checks

- [ ] G1 `scripts/smoke_studio.py` → 68/68 green against a throwaway repo.
- [ ] G2 Frontend: `npm run generate:api:check && npm run lint && npm run typecheck && npm test && npm run build` all green.
- [ ] G3 Backend: `uv run ruff check src/ tests/ scripts/ && uv run mypy --strict src/foundry/ && uv run pytest tests/` green. Known caveat: `test_placeholder_page_serves_when_no_frontend_built` requires the sibling `dist/` to be absent (see the 10c handoff § known issues).
- [ ] G4 No secrets anywhere in the browser: search devtools network responses for your real key value → zero hits (the contract test automates this; spot-check once by hand).
