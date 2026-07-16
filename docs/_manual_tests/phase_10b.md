# Phase 10b manual smoke test — Studio core screens

Prereqs: `cd ../agent-foundry-studio && npm install && npm run build`; then `cd ../agent-foundry && uv run foundry studio` and open http://127.0.0.1:4400. No API keys needed for any step (screens read local state; nothing here invokes an LLM).

1. **Shell + theme** — sidebar lists all screen areas; toggle dark/light; reload → choice persisted; OS-level scheme respected on first visit.
2. **Projects** — list shows hello, rag_hello, memory_hello, team_hello with health; open hello → agents/tools/connections/pins render with versions.
3. **Config editor** — open `projects/hello/agents/hello_agent/agent.yaml`; break it (`provider: anthropc`) → inline `L…:C…` diagnostic + panel error with did-you-mean + Save disabled; fix → Save → success toast shows the conventional commit subject; `git log -1` in the framework repo confirms the `studio(hello): …` commit.
4. **Catalog** — tools/connections/retrievers tabs list seeded artifacts with versions; open http_get_json → versions.json metadata + docs render.
5. **Doctor** — all checks render with status marks matching `uv run foundry doctor` output.
6. **Obs** — after any prior runs exist (`uv run foundry run hello --input '{"name":"Ada"}'` with keys, or the mock demo script), cost/latency/eval-trend charts and the runs feed populate and match `foundry obs cost --json`.
7. **Evals** — trigger hello's greeting.yaml eval (needs a provider key) → per-case table + score; history shows the run; matches `foundry eval` CLI output.
8. **Versions/rollback** — hello's commit list + per-artifact versions render; dry-run a prompt rollback → plan matches `foundry rollback --dry-run`; apply → commit visible in git.
9. **Runs / connections / storage** — runs history lists artifacts with status; connections health check executes; storage stats match `foundry storage stats --json`.
10. **No console errors** on any of the above (browser devtools).
