# Phase 1 — Manual Smoke Tests

**Phase scope**: `foundry.core` + provider abstraction (Anthropic + OpenAI) + YAML config loader + minimal LangGraph adapter + `foundry run` CLI against a trivial hello-world project.

**Reference**: [docs/03-development-phases.md § Phase 1](../03-development-phases.md) exit gate; [docs/_phase_handoffs/phase_1.md](../_phase_handoffs/phase_1.md) for implementation notes (including which env vars are needed and which models were used).

## Preconditions

- Phase 0 manual smoke test fully signed off.
- Claude Code review session for Phase 1 has reported **PASS**.
- Working tree is clean.
- You have valid API keys for **both** Anthropic and OpenAI (set as env vars below). If you don't, Phase 1 cannot be smoke-tested — get keys first.

## Setup

```bash
cd /Users/sam/projects/agent-foundry

# Required for provider swap test
export ANTHROPIC_API_KEY="sk-ant-..."
export OPENAI_API_KEY="sk-..."

# Recommended: use the cheapest model that satisfies the test for each provider
# (the handoff note documents the models Phase 1 was developed against — match those if possible)
```

Confirm `projects/hello/` exists with `system.yaml`, `state.yaml`, and `agents/hello_agent/{agent.yaml, prompts/v1.md, output_schema.py}`:

```bash
ls -la projects/hello/ projects/hello/agents/hello_agent/
```

## Tests

### Test 1 — Hello agent runs against Anthropic and produces a real greeting

**What we're verifying**: the end-to-end path — YAML → `SystemSpec` → compile → `langgraph_adapter` → Anthropic call → response → output — actually executes against a live provider.

**Run**:

```bash
uv run python -m foundry run projects/hello --input '{"name": "world"}'
```

**Expected**:
- Exit code 0.
- Output is a Greeting object (per `output_schema.py`) addressing the name in the input.
- A `run_id` is logged at the start of execution.
- An `~/.foundry/runs/<run_id>/` directory is created with the run artifact.

**If it fails**:
- `ProviderError` → check `ANTHROPIC_API_KEY` is exported in the same shell.
- `ConfigLoadError` → likely a schema drift between `projects/hello/` and the loader; fresh fix session.
- The greeting is empty or malformed → the prompt + output schema combination needs tuning; minor fix session.

- [ ] Pass

### Test 2 — Provider swap with ONE-LINE YAML change

**What we're verifying**: the provider abstraction's load-bearing claim — swapping providers requires only a config change, no code change.

**Run**:

```bash
# Edit projects/hello/agents/hello_agent/agent.yaml
# Change model_binding.provider: anthropic → openai
# Change model_binding.model: <anthropic model> → <openai model>
# Save.

uv run python -m foundry run projects/hello --input '{"name": "world"}'

# Revert when done
git checkout -- projects/hello/agents/hello_agent/agent.yaml
```

**Expected**:
- Exit code 0.
- Output is a Greeting matching the schema (possibly different prose vs. Anthropic — that's fine).
- The run artifact's metadata shows `provider: openai`.

**If it fails**:
- `ProviderError: unknown provider 'openai'` → adapter not registered; bug.
- `KeyError` deep in the stack → the swap leaked a provider-specific assumption somewhere; ARCHITECTURAL bug, escalate to a focused fix session.
- Output schema validation fails on OpenAI but not Anthropic → schema is too tight or there's a provider-specific normalization missing.

- [ ] Pass

### Test 3 — Unknown provider produces a useful error

**What we're verifying**: error message quality, not just that it errors.

**Run**:

```bash
# Edit agent.yaml: model_binding.provider: foo (any non-existent string)
uv run python -m foundry run projects/hello --input '{"name": "world"}' ; echo "exit=$?"
git checkout -- projects/hello/agents/hello_agent/agent.yaml
```

**Expected**:
- Non-zero exit code.
- Error message names the bad provider (`foo`), lists the available providers (`anthropic`, `openai`, possibly `bedrock`, `azure`, `vertex` as stubs), and identifies the file + field that caused it.
- No traceback dump — a structured error, not a Python exception printout.

**If it fails**: error message is unstructured / unhelpful → fresh fix session to improve the message. Quality matters here because it sets the tone for every future error.

- [ ] Pass

### Test 4 — YAML schema error is useful, not cryptic

**What we're verifying**: when a user fat-fingers a YAML file, the error helps them.

**Run**:

```bash
# Edit agent.yaml: rename the `model_binding` key to `model_bindings` (typo)
uv run python -m foundry run projects/hello --input '{"name": "world"}' ; echo "exit=$?"
git checkout -- projects/hello/agents/hello_agent/agent.yaml
```

**Expected**:
- Non-zero exit code.
- Error names the file (`projects/hello/agents/hello_agent/agent.yaml`), the field (`model_bindings` extra, `model_binding` missing), and ideally suggests the correction (Levenshtein hint per [docs/12-config-and-validation.md](../12-config-and-validation.md)).

**Cross-check** a second YAML break: introduce a syntax error (delete a `:` somewhere) and observe the error names the line + column.

**If it fails**: cryptic Pydantic dump (`field_required` with no file context) → loader isn't enriching the error per spec; fix session.

- [ ] Pass

### Test 5 — Cost budget enforcement fires PRE-CALL

**What we're verifying**: the budget is enforced before tokens are spent, not after the bill arrives.

**Run**:

```bash
# Edit projects/hello/system.yaml: set guardrails.max_cost_usd: 0.0001 (sub-call budget)
uv run python -m foundry run projects/hello --input '{"name": "world"}' ; echo "exit=$?"
git checkout -- projects/hello/system.yaml
```

**Expected**:
- Non-zero exit code.
- Error is `CostBudgetExceeded` (or whatever the impl chose).
- Message names the budget ($0.0001), the projected cost (whatever the LLM estimator predicted), and the agent that would have exceeded.
- **No actual LLM call was made** — verify by checking the run artifact (no `llm_calls.jsonl` entries) or by watching your provider dashboard (no usage recorded).

**If it fails**:
- Budget enforced after the call → architectural bug; the budget check is at the wrong point in the pipeline. Fresh fix session.
- Budget silently ignored → fix session.

- [ ] Pass

### Test 6 — Reasoning tokens captured for a reasoning model

**What we're verifying**: `TokenUsage.reasoning_tokens` is populated when the model emits reasoning, and zero when it doesn't.

**Run**:

```bash
# Edit agent.yaml: model_binding.model: <an OpenAI o-series model, e.g. o3-mini>
uv run python -m foundry run projects/hello --input '{"name": "world"}'
cat ~/.foundry/runs/<latest_run_id>/llm_calls.jsonl | jq '.token_usage'
git checkout -- projects/hello/agents/hello_agent/agent.yaml
```

**Expected**:
- `reasoning_tokens` field is present in the token usage record.
- Value is > 0 for the o-series call.

**Then re-run with a non-reasoning model** (`gpt-4o`, `claude-haiku-*`):
- `reasoning_tokens` is present and equals `0`.

**If it fails**:
- Field missing entirely → adapter isn't extracting it from the provider response; fix.
- Field is None or `null` → schema choice issue; should be `0`, not `None`, when no reasoning is reported.

**Skip with note** if you don't have o-series access — note "test skipped, no o-series access" in the sign-off and have the next phase's review session re-check.

- [ ] Pass (or [ ] Skipped: ____________)

### Test 7 — `run_id` threads through everything

**What we're verifying**: observability invariant — every log line, every span, every artifact reference contains the same `run_id`.

**Run**:

```bash
uv run python -m foundry run projects/hello --input '{"name": "world"}' 2>&1 | tee /tmp/foundry_run.log
grep -c 'run_id' /tmp/foundry_run.log
RUN_ID=$(grep -oE 'run_id[":= ]+[a-f0-9-]+' /tmp/foundry_run.log | head -1 | grep -oE '[a-f0-9-]+$')
echo "Found run_id: $RUN_ID"
ls ~/.foundry/runs/$RUN_ID/
```

**Expected**:
- The same `run_id` appears in multiple log lines AND in the artifact directory name.
- The artifact directory contains at minimum `metadata.json` referencing the same id.

**If it fails**: a log line without a `run_id` is missing the threading; fix session to wire it through whichever sub-call dropped it.

- [ ] Pass

### Test 8 — Secrets do NOT appear in logs or artifacts

**What we're verifying**: redactor / secret hygiene — your `ANTHROPIC_API_KEY` value must never end up on disk or stdout.

**Run**:

```bash
KEY_TAIL=${ANTHROPIC_API_KEY: -8}  # last 8 chars
uv run python -m foundry run projects/hello --input '{"name": "world"}' 2>&1 | tee /tmp/foundry_run.log
grep -F "$KEY_TAIL" /tmp/foundry_run.log ~/.foundry/runs/*/metadata.json ~/.foundry/runs/*/llm_calls.jsonl 2>/dev/null
echo "---"
echo "Above should be empty. Any output above is a secret leak."
```

**Expected**: no matches. Zero output between `---` and the final `echo`.

**If it fails**: **STOP, do not move to Phase 2.** Secret leak is a hard block — fresh fix session immediately. Verify redactor is wired into both the log handler and the artifact writer.

- [ ] Pass

### Test 9 — Adversarial import-boundary lint (still works after Phase 1 changes)

**What we're verifying**: Phase 1 added `foundry.runtime.langgraph_adapter` (the *only* legal `langgraph` importer). Lint must still block `langgraph` imports elsewhere.

**Run**:

```bash
echo "import langgraph  # deliberate" >> src/foundry/providers/__init__.py
uv run ruff check src/foundry/providers/ ; echo "exit=$?"
git checkout -- src/foundry/providers/__init__.py

# Repeat for foundry.core and foundry.config
```

**Expected**: each violation is flagged; exit code non-zero. After revert, lint is clean.

**If it fails**: a Phase 1 change loosened the boundary; fresh fix session.

- [ ] Pass

### Test 10 — Commit hygiene (Phase 1 commits)

**What we're verifying**: all Phase 1 commits use conventional format, no co-author line, no leakage.

**Run**:

```bash
git log --format="%h %s" main..HEAD  # or against the Phase 0 commit
git log --format="%b" main..HEAD | grep -i "co-authored-by" || echo "Clean — no co-author lines"
git log --format="%H%n%s%n%b%n---" $(git log --format="%H" -1 main..HEAD | tail -1)^..HEAD | grep -iE "citadel|client_x|firm_name_redacted" || echo "Clean — no leakage"
```

**Expected**:
- Every commit subject uses a conventional prefix.
- No `Co-Authored-By: Claude` anywhere.
- No firm-name / client-name leakage.

**If it fails**: amend or interactive-rebase to fix (this is one of the rare cases where a destructive git op is appropriate — confirm with operator before doing it).

- [ ] Pass

## Sign-off

When every box above is ticked:

- [ ] All 10 tests passed (Test 6 may be skipped with note).
- [ ] Both Anthropic AND OpenAI exercised end-to-end.
- [ ] Cost budget proven to fire pre-call.
- [ ] No secret leaks in any output or artifact.
- [ ] Ready to start Phase 2.

Signed off: ____________________ Date: __________

Add one-liner observations to `docs/_retros/phase_1.md` — especially anything in Tests 3 / 4 about error-message quality, since that compounds across every later phase.
