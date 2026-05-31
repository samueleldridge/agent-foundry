# Phase 2a — Manual Smoke Tests

**Phase scope**: `foundry.core.tool` + `foundry.core.connection` + `foundry.core.state` + complete tool/connection/state config schemas + `foundry.config.refs` + `foundry.catalog` (tools + connections) + `foundry.auth` (8 schemes) + `foundry.connections` (pool/registry/health) + `foundry.orchestration.state_scope` + per-tool/connection on-disk versioning. `hello_agent` is updated to call a catalog tool through a catalog connection.

**Reference**: [docs/03-development-phases.md § Phase 2a](../03-development-phases.md) exit gate; [docs/_phase_handoffs/phase_2a.md](../_phase_handoffs/phase_2a.md) for implementation notes (especially which catalog tools and connections were seeded and which versions exist).

## Preconditions

- Phase 1 manual smoke test fully signed off.
- Claude Code review session for Phase 2a has reported **PASS**.
- Working tree is clean; current branch is `main`.
- `ANTHROPIC_API_KEY` (or `OPENAI_API_KEY`, whichever the seeded `hello_agent` uses) is exported.
- Read `docs/_phase_handoffs/phase_2a.md` to know:
  - Which trivial catalog tools were seeded (you'll need their names).
  - Which connection the updated `hello_agent` binds to.
  - Whether the tool has both `v1` and `v2` on disk (Test 2 needs both).

## Setup

```bash
cd /Users/sam/projects/agent-foundry
ls catalog/tools/ catalog/connections/
ls projects/hello/ projects/hello/agents/hello_agent/
```

Confirm the catalog has at least 2 tool dirs and 3 connection dirs (postgres, pgvector, cohere_rerank), each with a versioned subdirectory and `versions.json`.

## Tests

### Test 1 — hello_agent runs end-to-end through a catalog tool + connection

**What we're verifying**: the load-bearing claim of Phase 2a — an agent can be configured (no code) to call a tool that resolves through the catalog, acquires a pooled+authenticated connection, returns a typed result, and the model continues with that result.

**Run**:

```bash
uv run python -m foundry run projects/hello --input '{"name": "world"}'
```

**Expected**:
- Exit code 0.
- Output is a Greeting object that demonstrably uses tool output (e.g., the seeded tool returns the current time, and the greeting mentions it).
- The run artifact at `~/.foundry/runs/<run_id>/` contains:
  - `tool_calls.jsonl` with at least one entry naming the tool + its version.
  - `connection.events` (or equivalent) showing a single acquire on the pool.
  - `metadata.json` referencing the pinned tool version and connection version.

**If it fails**:
- `ToolNotRegisteredError` → catalog index discovery is broken; fresh fix session.
- `ConnectionFactoryError` → auth scheme mis-wired; fresh fix session.
- Tool runs but result not picked up by the model → output_schema or tool→model contract is off; fix session.

- [ ] Pass

### Test 2 — Pin swap: tool v1 → v2 (and revert)

**What we're verifying**: the per-artifact versioning claim — bumping a pin in `system.yaml` causes the runtime to use a different on-disk tool version with NO other change.

**Preconditions**: the seeded catalog tool must have both `v1` and `v2` on disk (the handoff note should confirm; otherwise this test can't run and you need a fresh fix session to ship `v2`).

**Run**:

```bash
# Inspect the catalog tool's two versions
ls catalog/tools/<tool_name>/

# Verify current pin is v1
grep -A 2 '<tool_name>' projects/hello/system.yaml

# Edit projects/hello/system.yaml: bump the tool pin from v1 to v2
uv run python -m foundry run projects/hello --input '{"name": "world"}'

# Inspect: tool_calls.jsonl should show v2 was loaded
jq '.tool_version' ~/.foundry/runs/<latest>/tool_calls.jsonl

# Revert
git checkout -- projects/hello/system.yaml
```

**Expected**:
- The run after the pin bump uses `v2` (visible in `tool_calls.jsonl` and in any behavior difference v2 introduces).
- The run after the revert uses `v1` again.

**If it fails**:
- Same v1 behavior after pin bump → pin loader is caching or ignoring the change; fresh fix session.

- [ ] Pass

### Test 3 — Pin swap: connection v1 → v2

**What we're verifying**: the same versioning claim, but for connections (the other half of the pin).

**Preconditions**: a seeded catalog connection must have both `v1` and `v2`. If the impl session shipped only `v1`s, ask for a trivial `v2` of one connection before testing.

**Run**: same shape as Test 2, but bump a `connections.*.version` pin instead of a tool pin. Inspect `connection.events` in the run artifact.

**Expected**: connection events show the `v2` factory was used.

**If it fails**: same fix path as Test 2.

- [ ] Pass

### Test 4 — Tool NOT in agent allowlist → `ToolNotAllowedError`

**What we're verifying**: adversarial allowlist enforcement.

**Run**:

```bash
# Edit projects/hello/agents/hello_agent/agent.yaml
# Add a second tool to system.yaml.agents but DO NOT add it to the
# agent's allowed_tools list. (Or: remove the existing tool from the
# allowlist while leaving it in system.yaml.)
uv run python -m foundry run projects/hello --input '{"name": "world"}' ; echo "exit=$?"
git checkout -- projects/hello/agents/hello_agent/agent.yaml projects/hello/system.yaml
```

**Expected**:
- Non-zero exit code.
- `ToolNotAllowedError` (or equivalent) names the agent, the tool, and the allowlist.

**If it fails**: allowlist enforcement is at the wrong layer; fix session.

- [ ] Pass

### Test 5 — Connection slot unbound → compile-time `ConnectionSlotNotBoundError`

**What we're verifying**: adversarial compile-time check — a tool declaring a connection slot must have that slot bound in `system.yaml.connections` or the system refuses to compile.

**Run**:

```bash
# Edit projects/hello/system.yaml: delete or rename the connections.* binding
# that the catalog tool requires
uv run python -m foundry run projects/hello --input '{"name": "world"}' ; echo "exit=$?"
git checkout -- projects/hello/system.yaml
```

**Expected**:
- Non-zero exit code.
- `ConnectionSlotNotBoundError` names the slot AND the file + line where it was expected.
- The error is raised at COMPILE time (no LLM call was made — confirm via `~/.foundry/runs/`: no new run dir created, or one with `status: compile_failed`).

**If it fails**: error raised at runtime instead of compile time → state visibility/wiring validator is in the wrong pipeline stage; fix session.

- [ ] Pass

### Test 6 — Connection `accepts` mismatch → compile-time error

**What we're verifying**: a tool's connection slot declares `accepts: [postgres, pgvector]`; binding it to a `slack_workspace` ref should fail at compile time.

**Run**:

```bash
# Edit projects/hello/system.yaml: change a binding to point at a
# connection whose ref is NOT in the tool's accepts list
# (You may need to seed a second catalog connection of a different kind
# if only matching ones exist.)
uv run python -m foundry run projects/hello --input '{"name": "world"}' ; echo "exit=$?"
git checkout -- projects/hello/system.yaml
```

**Expected**: structured compile error names the slot, the tool, the `accepts` list, and the rejected ref.

**If it fails**: type validation is missing; fix session.

- [ ] Pass

### Test 7 — State visibility violation → compile-time `StateVisibilityError`

**What we're verifying**: an agent declaring `read: [messages]` cannot syntactically access `draft_plan`.

**Run**:

```bash
# Edit projects/hello/state.yaml: add a field draft_plan
# Edit projects/hello/agents/hello_agent/agent.yaml: do NOT add draft_plan
#   to the agent's reads/writes
# Edit the agent's prompt or output_schema to reference draft_plan in
# the agent's logic (you may need to add a temp output field that
# references state.draft_plan)
uv run python -m foundry run projects/hello --input '{"name": "world"}' ; echo "exit=$?"
git checkout -- projects/hello/
```

**Expected**:
- Non-zero exit at compile time.
- `StateVisibilityError` names the agent, the field, and the declared visibility.

**If it fails**:
- Runtime check instead of compile check → structural enforcement isn't structural; this is a P0 architectural bug per CLAUDE.md invariants. Fresh fix session.

- [ ] Pass

### Test 8 — Secret-literal scan catches credential in connection.config

**What we're verifying**: the safety net — if someone fat-fingers a credential into `system.yaml`'s `connections.*.config` block (instead of using env-var interpolation), the loader refuses.

**Run**:

```bash
# Edit projects/hello/system.yaml: under a connection binding, add a
# config value that looks like a credential, e.g.:
#   config:
#     api_key: "sk-ant-fake-credential-string-1234567890abcdef"
uv run python -m foundry run projects/hello --input '{"name": "world"}' ; echo "exit=$?"
git checkout -- projects/hello/system.yaml
```

**Expected**:
- Non-zero exit at load time.
- `ConfigLoadError` (or similar) names the file, the field, and explains that secret-looking literals are forbidden — must use `${ENV:NAME}` interpolation.

**If it fails**: secret scan didn't fire → security hole. Fresh fix session, high priority.

- [ ] Pass

### Test 9 — Connection health check CLI

**What we're verifying**: `foundry connections health` actually runs the connection's `health.yaml` eval.

**Run**:

```bash
uv run python -m foundry connections health projects/hello/<connection_name>
```

**Expected**:
- Exit code 0 on a healthy connection.
- Output indicates the eval ran (e.g., latency, status, checks performed).
- Non-zero exit + structured error if the connection is unreachable (you can test the failure path by breaking the auth env var temporarily).

**If it fails**: command not registered → fix session; eval ran but no useful output → ergonomics fix.

- [ ] Pass

### Test 10 — Connection pool reuses across tool calls

**What we're verifying**: when two tool calls in the same run share a connection slot, the pool returns the same client instance — verified by the run artifact's pool metrics (or events).

**Run**:

```bash
# Modify the hello_agent's prompt or use an input that causes the agent
# to call its tool TWICE in a single run.
# (Or: temporarily set iteration_limit > 1 and write a prompt that
# forces two tool calls.)
uv run python -m foundry run projects/hello --input '{"name": "world (twice)"}'

# Inspect the run artifact
jq '.connection_events | length' ~/.foundry/runs/<latest>/metadata.json
# OR
grep -c 'pool.acquire' ~/.foundry/runs/<latest>/connection.events
```

**Expected**:
- Two tool calls observable in `tool_calls.jsonl`.
- ONE `pool.acquire` event (or pool-reuse evidence) — not two.

**If it fails**:
- Two acquires → pool not caching; fix session.
- No `connection.events` artifact → instrumentation missing; fix session to wire the metrics.

- [ ] Pass

### Test 11 — Per-tool / per-connection version directory layout

**What we're verifying**: on disk, each tool and each connection has a versioned directory structure matching the spec — every version is immutable.

**Run**:

```bash
# Eyeball the on-disk shape
tree -L 3 catalog/tools/ catalog/connections/

# Check versions.json exists per artifact
find catalog/tools catalog/connections -name 'versions.json' | xargs -I {} sh -c 'echo "=== {} ==="; cat {}'

# Confirm v1 of a tool is immutable: try to modify, observe the issue
# (Actually: the immutability is enforced socially, not via fs perms,
# but the directory structure should be present and correct.)
```

**Expected**:
- Every tool has `<tool>/v1/{tool.yaml, handler.py, schemas.py, eval.yaml, README.md}` plus `<tool>/versions.json`.
- Every connection has `<conn>/v1/{connection.yaml, auth.py, schemas.py, health.yaml, README.md}` plus `<conn>/versions.json`.
- `versions.json` contains version metadata (created, eval_score if applicable, status).

**If it fails**: missing files → fix session.

- [ ] Pass

### Test 12 — Scope leakage check (Phase 2b/2c content NOT here)

**What we're verifying**: the implementation session stayed in scope and didn't ship 2b/2c work.

**Run**:

```bash
# These modules should NOT exist yet
ls src/foundry/cache/ src/foundry/retrieval/ src/foundry/memory/ \
   src/foundry/providers/embedders/ \
   src/foundry/core/cache.py src/foundry/core/retrieval.py \
   src/foundry/core/memory.py src/foundry/core/embedder.py \
   src/foundry/core/function_node.py src/foundry/core/node.py \
   2>&1 | grep -v 'No such'

# These ToolSpec fields should NOT exist yet
grep -nE 'cacheable|cache_ttl_s|cache_scope' src/foundry/config/schemas.py || echo "Clean — no cache fields in ToolSpec yet"

# These AgentSpec fields should NOT exist yet
grep -nE 'semantic_cache|retrievers|memory:' src/foundry/config/schemas.py || echo "Clean — no 2b/2c fields in AgentSpec yet"
```

**Expected**:
- The "should NOT exist" file checks produce ONLY "No such file" errors (you're inverting the grep so output is empty).
- The grep checks each produce only the "Clean — ..." message.

**If it fails**: out-of-scope leakage — fresh review session to confirm and decide whether to roll back or accept. Leakage is the failure mode the sub-phase split is designed to prevent; treat seriously.

- [ ] Pass

### Test 13 — Commit hygiene

Same as Phase 0/1 Test 8/10. Verify all Phase 2a commits use conventional format, no co-author lines, no leakage.

```bash
git log --format="%h %s" HEAD~10..HEAD  # adjust range to cover Phase 2a commits
git log --format="%b" HEAD~10..HEAD | grep -i "co-authored-by" || echo "Clean"
```

- [ ] Pass

## Sign-off

When every box above is ticked:

- [ ] All 13 tests passed.
- [ ] No out-of-scope leakage from 2b or 2c.
- [ ] Connection pool reuse demonstrably works.
- [ ] All compile-time errors fire at compile time (not runtime).
- [ ] Ready to start Phase 2b.

Signed off: ____________________ Date: __________

Add to `docs/_retros/phase_2a.md`: especially any compile-time check that surprised you (good or bad), since 2b and 2c will lean on the same validator infrastructure.
