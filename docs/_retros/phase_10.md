# Phase 10 retro — Foundry Studio (10a control plane · 10b screens · 10c real-time surfaces)

**What took longer than expected.** 10c was interrupted mid-flight (like
10b): a prior session had built most feature components (chat/graph/forge/
widgets) uncommitted, and the resuming session had to assess state before
finishing the dashboard host, routing, tests, and verification. The
mid-phase recovery pattern from docs/90 worked — commits are the source of
truth, and the uncommitted work was clean enough to keep wholesale.
React 19's stricter lint rules (`react-hooks/set-state-in-effect`,
`refs-during-render`, react-refresh purity) also caught five real issues in
the pre-built components that older lint configs would have let through.

**What changed from the plan.** (1) The GraphExport frontend type is a
hand-maintained mirror rather than a generated alias — the route returns a
JSONResponse (so it can 422 with a ValidationResult), leaving the OpenAPI
response `unknown`; acceptable for one schema, documented in the file.
(2) The wheel-packaging release hook (copy `dist/` into `_assets/`) was
deferred to the release session — 10c's backend fence (docs-only) beat the
deliverable line; everything else in the packaging story shipped in 10a.
(3) EventSource's missing wildcard listener forced an enumerated event
vocabulary in the SSE hook — a coupling between backend event kinds and
`sse.ts` worth a contract test if the vocabulary keeps growing.

**Found during the phase.** Building the frontend broke one 10a backend
test (`test_placeholder_page_serves_when_no_frontend_built`) — it depends
on the sibling `dist/` NOT existing, i.e. it was only ever green on
backend-only checkouts. Classic test-isolation bug; flagged in the 10c
handoff for a backend-side fix (tmp-dir asset resolution), deliberately not
fixed from the frontend session.

**What v1.2 should watch.** Code-split the bundle by route (1.98 MB single
chunk); drop the now-unused `@types/react-grid-layout`; consider Playwright
(tracked in docs/91) — the manual sheet at `docs/_manual_tests/phase_10.md`
is thorough but ~30 minutes per theme; wire the frontend repo to a remote
so the CI workflow actually runs.
