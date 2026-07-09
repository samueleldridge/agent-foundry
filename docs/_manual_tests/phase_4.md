# Phase 4 — Manual Smoke Tests

**Phase scope**: `foundry.eval` (schemas, harness, scorers, compare,
reporter) + `foundry eval` CLI (project / tool / agent / compare / show /
list / `--fail-under` / `--json`) + eval artifacts under
`~/.foundry/runs/<eval_run_id>/` + example eval sets (projects/hello,
catalog word_count v1/v2).

**Reference**: [docs/03-development-phases.md § Phase 4](../03-development-phases.md)
exit gate; [docs/_phase_handoffs/phase_4.md](../_phase_handoffs/phase_4.md)
for deviations (tool evals can't bind connections yet; deterministic mode
forces temperature 0; exit 2 = all cases errored).

## Preconditions

- Phase 3 manual smoke test fully signed off.
- Claude Code review session for Phase 4 has reported **PASS**.
- Working tree clean; `uv run pytest tests/` green (495).
- `ANTHROPIC_API_KEY` set; `OPENAI_API_KEY` set (Test 4 only).

## Setup

```bash
cd /Users/sam/projects/agent-foundry
export ANTHROPIC_API_KEY=...
evalruns() { ls -dt ~/.foundry/runs/* | head -1; }
```

## Tests

### Test 1 — Tool eval, no LLM (keys not needed)

**What we're verifying**: tool-scope harness + artifact, exit codes.

```bash
uv run python -m foundry eval tool catalog/word_count@v1; echo "exit=$?"
uv run python -m foundry eval tool catalog/word_count@v2; echo "exit=$?"
cat "$(evalruns)/eval_result.json" | head -30
```

**Expect**: both exit 0, `Score: 1.00 ... PASSED`; the artifact has
`eval_run_id`, `per_case` with `scorer_results`, `tokens_total: 0`.

### Test 2 — Live project eval with 5 cases + CI gate

**What we're verifying**: exit gate 1 + 7 — end-to-end eval against the
real provider; `--fail-under` behaviour.

```bash
uv run python -m foundry eval projects/hello evals/greeting.yaml \
  --fail-under 0.9; echo "exit=$?"
```

**Expect**: 5 cases run (real LLM calls), per-case details in the table,
score ≥ 0.9 → exit 0 (a live model reliably greets by name; if a case
fails, `Top failures:` names it). `total cost:` line shows real spend.
Then check the artifact + history read-back:

```bash
uv run python -m foundry eval list projects/hello
uv run python -m foundry eval show "$(basename "$(evalruns)")" | head -12
```

**Expect**: `list` shows the run with PASS; `show` re-renders the same
report from the artifact.

### Test 3 — Determinism (exit gate 6)

**What we're verifying**: same system + eval set + seed → same score
within tolerance (docs/40: ~99% case-level reproducibility at
temperature 0; the harness forces temperature 0 in deterministic mode).

```bash
uv run python -m foundry eval projects/hello evals/greeting.yaml --json \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['score'])"
uv run python -m foundry eval projects/hello evals/greeting.yaml --json \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['score'])"
```

**Expect**: identical scores across the two runs (regex scoring is
binary per case, so live-model drift would need a greeting that DROPS
the name — flag if the scores differ). Note: anthropic doesn't support
`seed`, so each run's events include a
`eval.determinism.seed_unsupported` warning — that's the documented
best-effort path.

### Test 4 — Cross-vendor LLM judge (exit gate 4)

**What we're verifying**: the judge goes through the provider
abstraction — anthropic-bound agent, openai-bound judge, in one eval.

```bash
export OPENAI_API_KEY=...
uv run python -m foundry eval projects/hello evals/greeting_judged.yaml --json \
  > /tmp/judged.json; echo "exit=$?"
python3 - << 'EOF'
import json
result = json.load(open("/tmp/judged.json"))
judge = [s for c in result["per_case"] for s in c["scorer_results"]
         if s["scorer_name"] == "warm_greeting_judge"]
print("judge verdicts:", [(s["score"], s["metadata"]["judge_provider"]) for s in judge])
print("rationales:", [s["metadata"]["rationale"][:60] for s in judge])
EOF
```

**Expect**: exit 0; every judge verdict shows `judge_provider: openai`
with a real rationale; case `tokens` include the judge's tokens; the
report footer flags `warm_greeting_judge` as `(non-deterministic)`.

### Test 5 — Agent-scope eval

```bash
uv run python -m foundry eval agent projects/hello hello_agent; echo "exit=$?"
```

**Expect**: exit 0; `scope: agent`, `Cases: 3 (... skipped: 1)` — the
documented `skip: true` case is reported but not scored.

### Test 6 — Cross-version tool comparison (exit gate 2)

```bash
uv run python -m foundry eval compare --tool word_count v1 v2; echo "exit=$?"
```

**Expect**: side-by-side v1/v2 columns (0.50 vs 1.00), `Fixes (2
case(s) flipped fail->pass)` naming `hyphenated_compound` +
`punctuation_only`, a persisted `eval_comparison.json` path printed.

### Test 7 — Cross-pin-set project comparison (exit gate 3)

**What we're verifying**: the same eval against the project at two git
refs, per-agent deltas, working tree untouched. HEAD vs HEAD~1 works on
any pair of commits that touched projects/hello; comparing a ref against
the live tree also works:

```bash
git -C . status --porcelain   # confirm clean before
uv run python -m foundry eval compare --project projects/hello \
  --pin-set HEAD --pin-set worktree \
  --eval projects/hello/evals/greeting.yaml; echo "exit=$?"
git -C . status --porcelain   # MUST still be clean after
```

**Expect**: exit 0; two columns (`HEAD` / `worktree`), a `Per-agent
breakdown:` block with `hello_agent`, scores near-identical (same pins);
`git status` unchanged (read-only overlay). For a real delta, repeat
with `--pin-set <sha-where-prompt-was-v1> --pin-set HEAD`.

### Test 8 — Failure + exit-code discrimination

```bash
# quality failure -> exit 1
uv run python -m foundry eval tool catalog/word_count@v1 \
  --eval catalog/tools/word_count/v2/eval.yaml; echo "exit=$?"
# infrastructure failure -> exit 2 (bad key: every case errors)
ANTHROPIC_API_KEY=broken uv run python -m foundry eval \
  projects/hello evals/greeting.yaml; echo "exit=$?"
```

**Expect**: first exits **1** (v1 fails v2's tokenisation contract;
`Top failures:` names the cases); second exits **2** with every case in
`error` status (`ProviderAuthError`) — CI can tell the two apart.

## Sign-off checklist

- [ ] Test 1 — tool evals pass, artifact readable
- [ ] Test 2 — live 5-case project eval + fail-under + show/list
- [ ] Test 3 — deterministic re-run reproduces the score
- [ ] Test 4 — cross-vendor judge with rationales
- [ ] Test 5 — agent-scope eval with skip accounting
- [ ] Test 6 — compare --tool side-by-side with flips
- [ ] Test 7 — compare --pin-set with per-agent deltas, tree untouched
- [ ] Test 8 — exit codes 1 vs 2 distinguishable
