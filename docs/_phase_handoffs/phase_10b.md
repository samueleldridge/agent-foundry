# Phase 10b handoff — Studio React foundation + core screens

**Date:** 2026-07-16 · **Repo:** `../agent-foundry-studio` (separate git; 6 commits `cccdb83..97b12af`) · **Status:** complete; ready for 10c.

## What shipped (frontend repo)

- Vite + React 19 + TypeScript strict + Tailwind v4 + shadcn/ui (Radix) + lucide; TanStack Query; React Router; CodeMirror 6; Recharts; sonner toasts.
- App shell: sidebar nav, breadcrumbs, dark/light theme (class-based, localStorage-persisted, `prefers-color-scheme` default), toaster, error boundaries.
- Typed API client over generated OpenAPI types (`npm run generate:api` against the running `foundry studio`; `generate:api:check` guards drift) + SSE helper.
- Core screens (table+detail, TanStack Query): projects list/detail (health, agents, tools, connections, pins), config editor (CodeMirror YAML/markdown/python; debounced server-side validation with pointer+line inline diagnostics; save-blocked-while-invalid; commit-on-save surfacing the conventional commit), catalog explorer (tools/connections/retrievers + versions + docs), doctor, obs dashboards (cost/latency/eval-trend/runs via Recharts), evals (run + results + history), versions/diff/rollback, runs history, connections health, storage stats, settings.
- Tests: 48 vitest tests across 15 files (testing-library + msw; `onUnhandledRequest: "error"`).

## Gates at close

`npm run build` ✓ (tsc + vite, 270ms) · `tsc --noEmit` ✓ · eslint ✓ · vitest 48/48 ✓ · `foundry studio` (no dev server) serves the production dist from the sibling checkout: index + SPA fallback + hashed assets + `/api/health` all 200 — verified.

## Notable fixes discovered during the phase

1. **Node ≥22 global `localStorage` shadowing** — Node's experimental `localStorage` global (undefined without `--localstorage-file`) shadows jsdom's Storage when vitest populates the test global; all 48 tests failed on `localStorage.clear()`. Fixed with a Storage bridge in `tests/setup.ts` (defineProperty getters to `globalThis.jsdom.window.localStorage/sessionStorage`). Upstream-worthy trivia for any vitest+jsdom repo on Node 22+.
2. msw doctor fixture was missing the required `ok` field (TS2741) — caught by the OpenAPI-generated types doing their job.
3. Config-editor test tightened to assert the validation message renders in BOTH the panel and inline diagnostics.

## Deviations

- Implementation was interrupted repeatedly (infra stalls); the orchestrator completed the final ~10% inline (vitest env fix, one test fix, README, commits, this handoff). All gate verification re-run from scratch afterwards.
- Screens ship without the 10c real-time surfaces (chat, flow graph, forge console, widget dashboards) per the phase fence.

## For 10c

- SSE helper in `src/api/sse.ts` is ready for chat/forge streams; `EventFeed` component renders RunEvent streams.
- Widget grid lib not yet installed (react-grid-layout goes in 10c).
- The generated types cover the full control plane incl. chat/graph/forge/layouts routes (10a shipped them); 10c is UI-only against existing endpoints.
