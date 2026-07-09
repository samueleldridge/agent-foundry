# Phase 6 demo — the meta-agent forges a project

Two ways to watch the forge work: the NO-KEY demo (run the exit-gate
integration test verbosely and read the artifacts it leaves behind) and
the LIVE demo (real Anthropic key, real reasoning — see the manual smoke
tests for the full checklist).

## Hero demo (no API key)

The integration harness scripts the meta-agent's LLM turns but everything
else is real: real meta-tools, real files, real commits, real evals in a
throwaway repo.

```bash
# 1) The whole exit gate, end to end (bootstrap → 0.5 → 0.833 → 1.0):
uv run pytest tests/integration/test_forge_toy.py -q

# 2) Guardrails: sandbox abort, cost cap, plateau, best-effort, rollback
uv run pytest tests/integration/test_forge_guards.py -q

# 3) The sandbox is structural, not prompt-level:
uv run pytest tests/unit/test_configurator_sandbox.py -q
```

## What to read afterwards

The hero test leaves a full trajectory artifact in its temp
`FOUNDRY_HOME`. To keep one around, run it with `--basetemp`:

```bash
uv run pytest tests/integration/test_forge_toy.py -q \
  --basetemp=/tmp/forge-demo
ls /tmp/forge-demo/*/foundry_home/runs/*/
#   meta.json  trajectory.jsonl  events.jsonl  final_summary.md
cat /tmp/forge-demo/*/foundry_home/runs/*/final_summary.md
```

`final_summary.md` shows the shape the operator sees after a live run:

```
# Forge 01K... — qa_bot

- Termination: threshold_met
- Final score: 1.000 (best 1.000; threshold 0.9)
- Iterations: 2 + bootstrap
...
## Trajectory
- iter 0 (bootstrap): - -> 0.500 | bootstrap | commits: <sha>
- iter 1 (iterate): 0.500 -> 0.833 | prompt_edit | commits: <sha>
- iter 2 (iterate): 0.833 -> 1.000 | prompt_edit | commits: <sha>
```

## CLI surface (live key required for a real run)

```bash
# New project: skeleton + foundry/<name> branch. NO system.yaml —
# that's the meta-agent's job.
uv run python -m foundry project new qa_bot

# The operator supplies the eval set (the target; the meta-agent can
# never write into evals/):
$EDITOR projects/qa_bot/evals/qa.yaml && git add -A && git commit -m "eval set"

# Forge:
uv run python -m foundry forge qa_bot \
  --description "Answer numeric questions: word counts, digit sums." \
  --eval projects/qa_bot/evals/qa.yaml \
  --threshold 0.9 --max-iter 5 --max-cost-usd 5
```

## What the demo proves

| Property | Where you see it |
|---|---|
| Catalog first: discovery + pinning | `list_catalog` turn; `catalog/word_count@v1` in system.yaml |
| Local tool: scaffold → standalone eval fail → fix → pass → wire | digit_sum's two tool-eval artifacts (0.0 then 1.0) |
| One commit per iteration, artifact + forge run id in the message | `git log foundry/qa_bot` in the temp repo |
| Eval is the judge, not the meta-agent's confidence | scores in trajectory.jsonl come from persisted eval artifacts |
| Sandbox aborts on out-of-project writes | `test_sandbox_violation_aborts_forge`: zero LLM calls after the attempt |
| Regression → compare_versions → rollback | `test_meta_agent_detects_regression_and_rolls_back`: `rollback(...)` commit + audit entry with `kind: meta_agent` |
| Budgets bound everything | cost-cap + plateau + max-iter terminations, each with its reason in `forge.terminated` |
