# Phase 9 demo — observability, dev UX, security, deploy (v1 complete)

Two variants: the NO-KEY scripted demo (the ≤5-minute exit-gate loop
against the mock provider) and the LIVE demo (real key + docker + OTel
collector — full checklist in `docs/_manual_tests/phase_9.md`).

## Hero demo (no API key): the whole loop in under a minute

```bash
uv run python scripts/demo_phase9.py
```

Only the provider HTTP layer is mocked (the same `httpx.MockTransport`
seam the test suite uses). Every other layer is real: real compile, real
eval harness, real FastAPI app, real git rollback, real SQLite mirror.
The mock LLM is *marker-gated* — it greets by name only while the pinned
prompt keeps its "addressed to that name" instruction — so step 4's
regression is a genuine behavioural regression caught by the real eval.

Recorded output (2026-07-13; total wall time **0.7s** against the
5-minute gate):

```
=== 1/5 forge a tiny project (bootstrap stand-in; live forge → manual §4) ===
project ready on branch foundry/hello (.../projects/hello)

=== 2/5 eval the project (foundry eval) ===
Eval: hello_greeting (scope: project; target: hello@c4427475e11e...)
Cases: 5 (passed: 5, failed: 0, skipped: 0)
Score: 1.00 (threshold: 0.90) PASSED

=== 3/5 serve the project + hit the API (real FastAPI app, POST /run) ===
POST /run -> 200
{ "greeting": "Hello, Foundry — wonderful to see you!" }
GET /health -> 200 alive

=== 4/5 ship a prompt regression, catch it with the eval, roll back ===
prompt v3 pinned; re-running the eval gate:
Score: 0.00 (threshold: 0.90) FAILED        ← regression caught
rolling back the prompt pin (foundry rollback --prompt):
  agent 'hello_agent' prompt: v3 -> v2
  Pre-flight checks: [ok] working_tree_clean [ok] correct_branch
                     [ok] no_inflight_runs   [ok] target_exists
Applied. Commit: 29a48615
Audit entry written (01KXEEQ0F1MWXXEDHG6T790YNH).
re-running the eval after rollback:
Score: 1.00 (threshold: 0.90) PASSED        ← recovered

=== 5/5 view cost metrics (foundry obs cost --project hello --since 1d) ===
model                       calls  input_tok  output_tok  cost_usd
------------------------------------------------------------------
anthropic:claude-haiku-4-5  16     960        288         0.0024

total: $0.002400

=== demo complete in 0.7s (gate: ≤ 5 minutes) ===
```

## The Phase 9 subsystems, exercised directly

```bash
# Observability: three transports cross-checked on one run (spans ==
# SQLite mirror == foundry obs == artifact files):
uv run pytest tests/integration/test_observability_exit_gate.py -q

# Contract: event-schema freeze + span attribute table + credential leak:
uv run pytest tests/contract/test_observability_schema.py \
              tests/integration/test_security_exit_gate.py -q

# Security: 50-case malicious-path fuzz + injection boundary:
uv run pytest tests/unit/test_security_sandbox_fuzz.py \
              tests/unit/test_security_injection.py -q

# Storage: backends round-trip, retention GC honours pins, archival:
uv run pytest tests/unit/test_storage_backends.py \
              tests/unit/test_storage_retention.py -q

# Review TUI (programmatic layer incl. rollback) + doctor:
uv run pytest tests/unit/test_tui_review.py tests/unit/test_cli_doctor.py -q

# Deploy: compute-version determinism, platform argv, eval gate, manifests:
uv run pytest tests/unit/test_deploy_compute_version.py \
              tests/unit/test_deploy_platforms.py \
              tests/unit/test_deploy_gate_and_recorder.py \
              tests/unit/test_deploy_manifests.py -q

# The CLI face of it all:
uv run foundry doctor
uv run foundry catalog list
uv run foundry obs cost --project hello --since 1d      # after the demo script
uv run foundry compute-version hello
uv run foundry deploy hello --image foundry-hello:demo --platform noop --skip-eval --dry-run
```

## Live variant

Real key, `docker build`, container + OTel collector via
`deploy/docker-compose.otel.yaml`, live forge, LangSmith opt-in:
`docs/_manual_tests/phase_9.md`.
