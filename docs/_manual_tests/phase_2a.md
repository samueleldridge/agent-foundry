# Phase 2a — Manual Smoke Tests

**Phase scope**: `foundry.core.tool` + `foundry.core.connection` + `foundry.core.state` + complete tool/connection/state config schemas + `foundry.config.refs` + `foundry.catalog` (tools + connections) + `foundry.auth` (8 schemes) + `foundry.connections` (pool/registry/health) + `foundry.orchestration.state_scope` + per-tool/connection on-disk versioning. `hello_agent` is updated to call a catalog tool through a catalog connection.

**Reference**: [docs/03-development-phases.md § Phase 2a](../03-development-phases.md) exit gate; [docs/_phase_handoffs/phase_2a.md](../_phase_handoffs/phase_2a.md) for implementation notes and deviations.

## Preconditions

- Phase 1 manual smoke test fully signed off.
- Claude Code review session for Phase 2a has reported **PASS**.
- Working tree is clean; current branch is `main`.
- Env vars exported:

```bash
export ANTHROPIC_API_KEY="sk-ant-..."
export HELLO_SERVICE_API_KEY=dummy      # worldtimeapi ignores auth; the key is still injected
# optional: export HELLO_TIME_BASE_URL=...   # defaults to https://worldtimeapi.org
```

- Seeded artifacts (from the handoff): tools `utc_now@v1`, `word_count@v1`, `http_get_json@v1+v2`; connections `http_service@v1+v2`, `postgres@v1`, `pgvector@v1`, `cohere_rerank@v1`. hello binds tool `get_time` = `catalog/http_get_json@v1` and connection `time_service` = `catalog/http_service@v1`.

## Setup

```bash
cd <repo-root>
ls catalog/tools/ catalog/connections/
cat catalog/index.yaml
ls projects/hello/ projects/hello/agents/hello_agent/
```

Confirm the catalog has 3 tool dirs and 4 connection dirs (http_service, postgres, pgvector, cohere_rerank), each with versioned subdirectories, `versions.json`, and `LATEST`.

## Tests

### Test 1 — hello_agent runs end-to-end through a catalog tool + connection

**What we're verifying**: the load-bearing claim of Phase 2a — an agent configured with no code calls a tool that resolves through the catalog, acquires a pooled+authenticated connection, returns a typed result, and the model continues with it.

**Run**:

```bash
uv run python -m foundry run projects/hello --input '{"name": "world"}'
```

**Expected**:
- Exit code 0.
- Output is a Greeting that demonstrably uses tool output (the greeting mentions the current time; live model prose varies).
- The run artifact at `~/.foundry/runs/<run_id>/` contains:
  - `tool_calls.jsonl` with one entry: `"tool_ref": "catalog/http_get_json"`, `"tool_version": "v1"`, `"success": true`.
  - `events.jsonl` with a `"event": "connection"` record, `"lifecycle": "acquire"`, descriptor `ref catalog/http_service@v1`, slot `service` — and NO credential material anywhere.
  - `metadata.json` with `pins.tools.get_time = catalog/http_get_json@v1`, `pins.connections.time_service = catalog/http_service@v1`, and a `connection_pool` block with `builds: 1`.

**If it fails**:
- `RefResolutionError` → catalog discovery broken; fresh fix session.
- `ConnectionAuthError`/`ConnectionConfigError` at build → factory mis-wired; fix session.
- Tool runs but the greeting ignores it → prompt/contract issue; note it, not necessarily blocking (model choice).

- [ ] Pass

### Test 2 — Pin swap: tool v1 → v2 (and revert)

**What we're verifying**: bumping a pin in `system.yaml` makes the runtime use a different on-disk tool version with NO other change.

**Run**:

```bash
ls catalog/tools/http_get_json/           # v1, v2, versions.json, LATEST
grep -A 2 'get_time' projects/hello/system.yaml   # pin is v1

# Edit projects/hello/system.yaml: get_time version v1 → v2
uv run python -m foundry run projects/hello --input '{"name": "world"}'
tail -1 ~/.foundry/runs/$(ls -t ~/.foundry/runs | head -1)/tool_calls.jsonl | jq .tool_version

git checkout -- projects/hello/system.yaml
```

**Expected**: `"v2"` after the bump (v2's output also carries the resolved request `url`); `"v1"` again after revert.

- [ ] Pass

### Test 3 — Pin swap: connection v1 → v2 (auth-scheme swap)

**What we're verifying**: the same versioning claim for connections — v2 of `http_service` uses basic auth instead of an API-key header; the tool is untouched.

**Run**:

```bash
# Edit projects/hello/system.yaml: time_service version v1 → v2
export HELLO_SERVICE_API_KEY='{"username":"u","password":"p"}'   # v2 = basic auth JSON creds
uv run python -m foundry run projects/hello --input '{"name": "world"}'
jq .pins.connections ~/.foundry/runs/$(ls -t ~/.foundry/runs | head -1)/metadata.json

git checkout -- projects/hello/system.yaml
export HELLO_SERVICE_API_KEY=dummy
```

**Expected**: run succeeds; metadata pins show `catalog/http_service@v2`; the `connection` event's descriptor shows `auth_scheme: basic_auth`.

- [ ] Pass

### Test 4 — Tool not in allowlist → dispatcher refuses (docs/20 semantics)

**What we're verifying**: allowlist enforcement at dispatch. Per docs/20 § Allowlisting, a refused call does NOT abort the run — the LLM receives a structured `ToolNotAllowedError` tool_result and recovers. (The adversarial hallucinated-tool-call path is pinned by `tests/integration/test_run_hello_tools.py::test_tool_not_in_allowlist_refused_and_surfaced_to_llm`; live models won't reliably hallucinate a tool they weren't offered.)

**Run**:

```bash
# Edit projects/hello/agents/hello_agent/agent.yaml: tools: [get_time] → tools: []
uv run python -m foundry run projects/hello --input '{"name": "world"}' ; echo "exit=$?"
ls ~/.foundry/runs/$(ls -t ~/.foundry/runs | head -1)/
git checkout -- projects/hello/agents/hello_agent/agent.yaml
```

**Expected**:
- Exit 0; the greeting arrives WITHOUT the time (the tool was never advertised).
- No `tool_calls.jsonl` in the run dir (no dispatch happened).
- Bonus compile check: allowlisting a tool that isn't in `system.yaml.tools` (e.g. `tools: [get_time, ghost]`) exits 2 with a CompileError naming `ghost`.

- [ ] Pass

### Test 5 — Connection slot unbound → compile-time `ConnectionSlotNotBoundError`

**Run**:

```bash
# Edit projects/hello/system.yaml: delete the two lines
#     connection_bindings:
#       service: time_service
uv run python -m foundry run projects/hello --input '{"name": "world"}' ; echo "exit=$?"
git checkout -- projects/hello/system.yaml
```

**Expected**:
- Exit 2. `ConnectionSlotNotBoundError` names the slot (`service`), the file (`system.yaml`), the pointer, and a hint.
- COMPILE time: no new run dir under `~/.foundry/runs/`.

- [ ] Pass

### Test 6 — Connection `accepts` mismatch → compile-time error

**Run**:

```bash
# Edit projects/hello/system.yaml:
#   change `service: time_service` → `service: reranker`, and add under connections:
#     reranker:
#       ref: catalog/cohere_rerank
#       version: v1
#       credentials_ref: { kind: env, value: FAKE_COHERE_KEY }
export FAKE_COHERE_KEY=dummy
uv run python -m foundry run projects/hello --input '{"name": "world"}' ; echo "exit=$?"
git checkout -- projects/hello/system.yaml
```

**Expected**: exit 2; CompileError names the tool, the slot, the `accepts` list (`catalog/http_service`), and the rejected ref (`catalog/cohere_rerank@v1`).

- [ ] Pass

### Test 7 — State visibility violation → compile-time `StateVisibilityError`

**Run**:

```bash
# Edit projects/hello/state.yaml: add under schema:
#   draft_plan:
#     type: str | None
# Edit projects/hello/agents/hello_agent/agent.yaml:
#   read: [name]  →  read: [name, draft_plan]
# (state.yaml's visibility for hello_agent still grants only [name])
uv run python -m foundry run projects/hello --input '{"name": "world"}' ; echo "exit=$?"
git checkout -- projects/hello/
```

**Expected**:
- Exit 2 at compile time; `StateVisibilityError` names the agent, the field, and both declared scopes.
- Also try: rename `hello_agent:` in state.yaml's visibility block → `StateVisibilityError` about the missing entry.
- The structural claim (forbidden fields literally absent from the agent's view) is pinned by `tests/unit/test_state_scope.py::test_projection_omits_forbidden_fields_structurally`.

- [ ] Pass

### Test 8 — Secret-literal scan catches credential in connection.config

**Run**:

```bash
# Edit projects/hello/system.yaml: under time_service.config add:
#     api_key: "sk-ant-fake-credential-string-1234567890abcdef"
uv run python -m foundry run projects/hello --input '{"name": "world"}' ; echo "exit=$?"
git checkout -- projects/hello/system.yaml
```

**Expected**: exit 2 at LOAD time; `ConfigLoadError` names the file + pointer, says secret literals are forbidden (use env/credentials_ref), and does NOT echo the value.

- [ ] Pass

### Test 9 — Connection health check CLI

**Run**:

```bash
uv run python -m foundry connections health projects/hello/time_service ; echo "exit=$?"
# failure path: break the base_url
HELLO_TIME_BASE_URL=https://nonexistent.invalid \
  uv run python -m foundry connections health projects/hello ; echo "exit=$?"
```

**Expected**:
- Healthy: exit 0; output shows the connection, its ref, per-case status + latency (`[ok] ping (…ms) — GET /api/ip -> 200`).
- Broken: exit 1 with `ConnectionHealthCheckError` + per-case details.
- Unknown name (`projects/hello/nope`): exit 2.

- [ ] Pass

### Test 10 — Connection pool reuses across tool calls

**What we're verifying**: two tool calls sharing a connection slot in one run reuse the same client (pool metrics in the artifact).

**Run**:

```bash
# Temporarily edit projects/hello/agents/hello_agent/prompts/v2.md to instruct
# TWO get_time calls (e.g. "call get_time twice: once for /api/timezone/Etc/UTC
# and once for /api/ip"), then:
uv run python -m foundry run projects/hello --input '{"name": "world"}'
jq '.connection_pool' ~/.foundry/runs/$(ls -t ~/.foundry/runs | head -1)/metadata.json
wc -l ~/.foundry/runs/$(ls -t ~/.foundry/runs | head -1)/tool_calls.jsonl
git checkout -- projects/hello/
```

**Expected**:
- Two entries in `tool_calls.jsonl` (model willing; retry with a firmer prompt if it calls once).
- `connection_pool.builds == 1` and `cache_hits >= 1`; events.jsonl shows one `lifecycle: acquire` + one `lifecycle: cache_hit`.
- (Pinned deterministically by `test_pool_reuse_across_two_tool_calls_in_one_run`.)

- [ ] Pass

### Test 11 — Per-tool / per-connection version directory layout

**Run**:

```bash
find catalog/tools catalog/connections -maxdepth 2 | sort
find catalog -name 'versions.json' -exec sh -c 'echo "=== $1 ==="; cat "$1"' _ {} \;
```

**Expected**:
- Every tool has `<tool>/v<N>/{tool.yaml, handler.py, schemas.py, eval.yaml, README.md}` + `<tool>/versions.json` + `LATEST`.
- Every connection has `<conn>/v<N>/{connection.yaml, auth.py, schemas.py, health.yaml, README.md}` + `versions.json` + `LATEST`.
- `versions.json` entries list exactly the versions on disk (created_at, created_by, notes).

- [ ] Pass

### Test 12 — Scope leakage check (Phase 2b/2c content NOT here)

**What we're verifying**: the implementation stayed in scope. NOTE: `src/foundry/core/{cache,retrieval,memory,embedder,node,function_node}.py` DO exist — they are Phase 1 protocol stubs (see the phase_1 handoff) and that is expected. What must NOT exist is 2b/2c *implementation*.

**Run**:

```bash
# Implementation packages must still be docstring-only stubs (1 line each):
wc -l src/foundry/cache/*.py src/foundry/retrieval/*.py src/foundry/memory/*.py
ls src/foundry/providers/embedders 2>&1 | grep -v 'No such' ; echo "---"

# These ToolSpec/AgentSpec FIELD DECLARATIONS must NOT exist yet
# (docstrings mentioning the deferred names are fine):
grep -nE '^\s+(cacheable|cache_ttl_s|cache_scope):' src/foundry/config/schemas.py \
  || echo "Clean — no cache fields in ToolSpec yet"
grep -nE '^\s+(semantic_cache|retrievers|memory):' src/foundry/config/schemas.py \
  || echo "Clean — no 2b/2c fields in AgentSpec yet"
```

**Expected**: cache/retrieval/memory modules are one-line docstrings; no `providers/embedders/`; both greps print their "Clean" message.

**If it fails**: out-of-scope leakage — fresh review session to confirm and decide whether to roll back or accept.

- [ ] Pass

### Test 13 — Commit hygiene

```bash
git log --format="%h %s" 6d276d3..HEAD
git log --format="%b" 6d276d3..HEAD | grep -i "co-authored-by" || echo "Clean"
```

Verify conventional format, no co-author lines, no institution-name leakage.

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
