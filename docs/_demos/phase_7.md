# Phase 7 demo — supervisor + workers + a human in the loop

Two ways to watch it: the NO-KEY demo (run the exit-gate integration
tests verbosely and read the artifacts) and the LIVE demo (real Anthropic
key against `projects/team_hello` — full checklist in
`docs/_manual_tests/phase_7.md`).

## Hero demo (no API key)

The harness scripts the LLM turns per agent (routed by system prompt),
but everything else is real: real compile with handoff-tool synthesis,
real routers, real sqlite checkpointer, real interrupt/resume.

```bash
# 1) Supervisor + 2 workers + HITL pause/approve/reject + kill+resume:
uv run pytest tests/integration/test_run_team_hello.py -q

# 2) Parallel fan-out/fan-in under REAL concurrency, graph routing,
#    nested flows, max_hops policy matrix, max_iterations:
uv run pytest tests/integration/test_run_multi_agent_flows.py -q

# 3) The predicate sandbox + handoff/HITL contracts + plan compiler:
uv run pytest tests/unit/test_orchestration_predicates.py \
              tests/unit/test_orchestration_handoff_hitl.py \
              tests/unit/test_orchestration_patterns.py -q
```

## What to read afterwards

Keep a run's artifacts around with `--basetemp`:

```bash
uv run pytest tests/integration/test_run_team_hello.py -q \
  --basetemp=/tmp/team-demo -k approve
ls /tmp/team-demo/*/foundry_home/runs/*/
#   metadata.json  events.jsonl  llm_calls.jsonl  tool_calls.jsonl
#   final_state.json
```

`events.jsonl` shows the full audit trail across the pause — note the
sequence numbers CONTINUE through the resume:

```
run.started → agent.started(coordinator) → llm.* →
handoff(coordinator→drafter, llm, hop 1) → agent.*(drafter) →
handoff(drafter→coordinator, rule, hop 2) → ... →
handoff(coordinator→publisher, llm, hop 3) → llm.* →
approval.required(publish-<run_id>-<hash>) →
run.completed(status=approval_pending)
--- operator: foundry resume <run_id> --approve ---
approval.resolved(approved) → run.started(resumed) → tool.* → llm.* →
handoff(publisher→coordinator, rule, hop 4) → ... →
handoff(coordinator→END, end, hop 5) → run.completed(status=success)
```

## Live demo (API key required)

```bash
export ANTHROPIC_API_KEY=...
uv run python -m foundry run projects/team_hello \
  --input '{"request": "the new release shipping", "audience": "the team"}' \
  --checkpoint sqlite --stream
# → run pauses: "run paused: approval required", prints the run id
uv run python -m foundry approvals list
uv run python -m foundry resume <RUN_ID>            # show the prompt
uv run python -m foundry resume <RUN_ID> --approve  # → final summary JSON
```
