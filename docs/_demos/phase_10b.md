# Phase 10b demo — core screens through `foundry studio`

```bash
# frontend (sibling repo)
cd ../agent-foundry-studio && npm install && npm run build
# backend serves API + built SPA from one port
cd ../agent-foundry && uv run foundry studio --port 4411
```

Verified 2026-07-16 against the production build (no dev server):

```
GET /                     → 200, <title>Foundry Studio</title>
GET /projects             → 200 (SPA history fallback)
GET /assets/index-*.js    → 200 (hashed bundle)
GET /api/health           → {"status":"ok","version":"0.1.0",...}
```

Frontend gates at the same commit: vitest 48/48 · `tsc --noEmit` clean · eslint clean · `vite build` 270ms. Browser walk-through (projects → edit a config with a deliberate typo → inline `L3:C13 unknown provider 'anthropc'` + disabled save → fix → save commits) is captured as the Phase 10b section of `docs/_manual_tests/phase_10.md`-series checklists.
