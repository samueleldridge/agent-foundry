# Phase 9 — Manual Smoke Tests (final phase; v1 gate)

**Phase scope**: `foundry.observability` (tracing/metrics/store/exporters),
`foundry.storage`, `foundry.security`, `foundry.testing`, `foundry obs` /
`storage` / `test` / `doctor` / `review` / `deploy` / `compute-version` /
`catalog list|show` CLIs, Dockerfile + platform manifests.

**Reference**: [docs/03-development-phases.md § Phase 9](../03-development-phases.md)
exit gate; [docs/_phase_handoffs/phase_9.md](../_phase_handoffs/phase_9.md)
for deviations. Steps 2–5 exist here because the dev sandbox had **no
Docker daemon, no live OTel collector, and no API keys** — everything
else was verified by the automated suite.

## Preconditions

- Phase 8 manual smoke test signed off; Phase 9 AI review PASS.
- Working tree clean; `uv run pytest tests/` green (983+ passed).
- Docker Desktop (or equivalent) running for steps 2–3.
- A real `ANTHROPIC_API_KEY` for steps 4–6 (~$0.50 at Haiku pricing;
  the forge step uses Opus — budget ~$2 with `--max-cost-usd`).

## 1. Scripted no-key demo (baseline sanity, ~1 minute)

```bash
uv run python scripts/demo_phase9.py
```

- [ ] All five steps pass; total time printed well under 5 minutes.
- [ ] `foundry obs cost` table at the end shows a non-zero cost total.

## 2. docker build + run (no keys needed for the health check)

```bash
docker info                       # daemon up
docker build -t foundry-api:$(uv run foundry compute-version hello) .
docker run --rm -p 8080:8080 \
  -e ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY \
  -e HELLO_SERVICE_API_KEY=anything \
  -e FOUNDRY_ENV=dev \
  foundry-api:$(uv run foundry compute-version hello)
# separate shell:
curl -s localhost:8080/health
curl -s -X POST localhost:8080/run -H 'content-type: application/json' \
  -d '{"name": "operator"}'
```

- [ ] Image builds from the repo Dockerfile without edits.
- [ ] `docker inspect` shows the `foundry.project` / `foundry.system_version` labels.
- [ ] `/health` returns 200 `alive`; POST /run returns a Greeting (live key).
- [ ] Container runs as a non-root user (`docker exec <id> id` → uid 1001).

## 3. OTel export to a container-side collector

```bash
docker compose -f deploy/docker-compose.otel.yaml up --build
# separate shell — drive some traffic:
curl -s -X POST localhost:8080/run -H 'content-type: application/json' \
  -d '{"name": "otel"}'
docker compose -f deploy/docker-compose.otel.yaml logs otel-collector | tail -50
```

- [ ] Collector logs show `foundry.run`, `foundry.node`, `foundry.llm`
      spans (debug exporter prints span names + attributes).
- [ ] Span attributes include `run_id`, `project`, `worker_id`; NO
      credential-shaped values anywhere in the collector output.
- [ ] Metrics pipeline receives `foundry.run.total` / `foundry.llm.cost_usd`.

## 4. Live 5-minute demo (real forge → eval → serve → rollback → cost)

```bash
export ANTHROPIC_API_KEY=...          # real key
time (
  uv run foundry project new demo_qa
  uv run foundry forge demo_qa \
    --description "A numeric-answer QA agent: answer 'digitsum: <digits>' questions" \
    --eval projects/demo_qa/evals/qa.yaml --threshold 0.8 --max-cost-usd 2 \
    || true                            # authoring the eval first is part of the flow
  uv run foundry eval demo_qa evals/qa.yaml
  uv run foundry serve demo_qa --port 8080 &
  sleep 3 && curl -s -X POST localhost:8080/run -d '{"question":"digitsum: 123"}' \
    -H 'content-type: application/json'
  kill %1
  uv run foundry versions demo_qa
  uv run foundry rollback demo_qa --to <known-good-commit> --yes
  uv run foundry obs cost --project demo_qa --since 1d
)
```

- [ ] Total wall time ≤ 5 minutes.
- [ ] `foundry obs cost --project demo_qa --since 1d` shows the forge +
      eval + serve spend broken down by model.
- [ ] `foundry review demo_qa` lists the forge commits with eval deltas;
      `r` + typing the sha rolls back; `q` quits cleanly.

## 5. LangSmith opt-in exporter (docs/80)

```bash
export FOUNDRY_TRACING=langsmith LANGSMITH_API_KEY=... LANGSMITH_PROJECT=foundry-smoke
uv run foundry run projects/hello --input '{"name":"langsmith"}'
```

- [ ] Trace tree (foundry.run → node → llm) appears in the LangSmith UI.
- [ ] Unsetting `LANGSMITH_API_KEY` with `FOUNDRY_TRACING=langsmith`
      fails fast with a structured ConfigError naming the missing env var.

## 6. Storage lifecycle against real artifacts

```bash
uv run foundry storage stats
uv run foundry storage pin run <run_id_from_step_4> --reason "phase 9 signoff"
uv run foundry storage gc --kind runs --older-than 0d --dry-run
uv run foundry storage gc --kind runs --older-than 0d
uv run foundry storage list-pinned
uv run foundry storage archive --kind runs --older-than 0d
```

- [ ] dry-run lists candidates without deleting; real gc deletes them.
- [ ] The pinned run SURVIVES gc; `--force` deletes it and logs loudly.
- [ ] Archive produces `~/.foundry/archives/runs-<yyyy>-<mm>.tar.gz`.

## 7. Doctor + onboarding pass (docs/82 exit gate)

From a **fresh clone** in a fresh venv:

```bash
git clone <repo> /tmp/foundry-fresh && cd /tmp/foundry-fresh
uv sync
uv run foundry doctor
uv run foundry test projects/hello        # if the project ships tests/
uv run foundry run projects/hello --input '{"name":"onboarding"}'
```

- [ ] Whole flow under 10 minutes; doctor exits 0 (warnings OK, named).

## Sign-off

- [ ] All boxes above checked.
- [ ] `git tag v1.0.0` exists locally; push tags only after this sheet
      passes (`git push --tags`).
