# Phase 4 demo — the eval harness: three scopes, one runner, comparable artifacts

## Hero commands

```bash
export ANTHROPIC_API_KEY="sk-ant-..."   # live-key step — PENDING OPERATOR
export OPENAI_API_KEY="sk-..."          # only for the judged eval

# 1) End-to-end project eval: 5 cases, deterministic, CI-gated
uv run python -m foundry eval projects/hello evals/greeting.yaml --fail-under 0.9

# 2) Tool eval — no agent, no LLM, no keys needed
uv run python -m foundry eval tool catalog/word_count@v1

# 3) Cross-version tool comparison (v2's eval against both versions)
uv run python -m foundry eval compare --tool word_count v1 v2

# 4) Cross-pin-set project comparison (current vs previous commit)
uv run python -m foundry eval compare --project projects/hello \
  --pin-set HEAD~1 --pin-set HEAD --eval projects/hello/evals/greeting.yaml

# 5) Cross-vendor LLM judge (anthropic agent, openai judge)
uv run python -m foundry eval projects/hello evals/greeting_judged.yaml

# 6) Read results back (the Phase 6 surface)
uv run python -m foundry eval list projects/hello
uv run python -m foundry eval show <EVAL_RUN_ID> --json | head -40
```

## Representative output

Captured from the dev sandbox — tool evals are fully real; LLM-touching
runs used `httpx.MockTransport` (**no API keys exist in the sandbox**),
so greetings are canned but the harness/scoring/artifact path is the
real one.

### Tool eval (`foundry eval tool catalog/word_count@v1`)

```
Eval: word_count_v1_eval (scope: tool; target: catalog/word_count@v1)
Cases: 2 (passed: 2, failed: 0, skipped: 0)
Score: 1.00 (threshold: 1.00) PASSED
Duration: 0.0s

Per-scorer:
  exact_match                  avg 1.00  pass% 1.00

Run artifact: ~/.foundry/runs/01KX2VSHR01ZM437GKKWE5JRF5/eval_result.json
```

### Cross-version comparison (`compare --tool word_count v1 v2`)

```
Eval: word_count_v2_eval (spec efdc04ff011bd943)
Target: catalog/word_count
Cases: 4

                        v1        v2
Score                 0.50      1.00  (Δ +0.50)
Pass rate              2/4       4/4

Fixes (2 case(s) flipped fail->pass):
  hyphenated_compound
  punctuation_only

Comparison artifact: ~/.foundry/runs/01KX2VT8472BDC7A45819J56MV/eval_comparison.json
```

### Project eval (mock transport)

```
Eval: hello_greeting (scope: project; target: hello@b3b66d3e...)
Cases: 5 (passed: 5, failed: 0, skipped: 0)
Score: 1.00 (threshold: 0.90) PASSED
Duration: 0.2s; total cost: $0.00075

Per-scorer:
  greeting_mentions_name       avg 1.00  pass% 1.00

Run artifact: ~/.foundry/runs/01KX2VTY6T30CVVKNQ68A7JFWQ/eval_result.json
```

### Pin-set comparison (temp git repo, prompt pin v1 → v2; mock transport)

```
Eval: hello_greeting (spec f692fa99eccec079)
Target: hello
Cases: 5

                    HEAD~1      HEAD
Score                 0.00      1.00  (Δ +1.00)
Pass rate              0/5       5/5
Cost (USD)         0.00075   0.00075

Per-agent breakdown:
  hello_agent                   0.00      1.00  (+1.00)

Fixes (5 case(s) flipped fail->pass):
  plain_name
  short_name
  full_name
  lowercase_name
  heavier_weighted_name
```

## Why this matters

- **One harness, three scopes.** The same EvalSpec/scorers/reporter run a
  bare tool, an agent in isolation, or the whole compiled system — the
  meta-agent (Phase 6) gets one signal shape everywhere.
- **Results are artifacts, not printouts.** Every run persists a typed
  `EvalRunResult` under `~/.foundry/runs/<eval_run_id>/`; `compare` is a
  pure function over them, guarded by the spec content hash.
- **CI-ready exit codes.** 0 = pass, 1 = below `--fail-under`, 2 = the
  eval itself could not run — regressions and broken plumbing are
  distinguishable in a pipeline.
- **The judge is provider-agnostic.** `llm_judge` takes a ModelBinding;
  cross-vendor judging (reduced co-bias) is a YAML choice, not code.
