# Phase 3 — Manual Smoke Tests

**Phase scope**: `foundry.orchestration.compiler` + `.patterns` (single +
one-agent sequential; Phase 7 stubs for the rest) + graph-native agent
step (tool loop + memory turn loop as StateGraph nodes) +
`foundry.runtime.checkpointers` (memory + SQLite) + kill+resume +
`foundry run --stream / --checkpoint / --run-id` + `foundry.run` /
`foundry.node` / `foundry.llm` trace spans.

**Reference**: [docs/03-development-phases.md § Phase 3](../03-development-phases.md)
exit gate; [docs/_phase_handoffs/phase_3.md](../_phase_handoffs/phase_3.md)
for deviations (RunEvent-level streaming; completed-thread rerun = fresh
run; `run.started` emitted once per process on resume).

## Preconditions

- Phase 2c manual smoke test fully signed off.
- Claude Code review session for Phase 3 has reported **PASS**.
- Working tree clean; `uv run pytest tests/` green (429).
- `ANTHROPIC_API_KEY` set (hello + memory_hello need nothing else).

## Setup

```bash
cd <repo-root>
export ANTHROPIC_API_KEY=...
runs() { ls -d ~/.foundry/runs/* | tail -1; }
```

## Tests

### Test 1 — Live streaming run (`--stream`)

**What we're verifying**: incremental RunEvent output through the graph
path, typed output last, run completes with a checkpointer attached
(default `memory`).

```bash
uv run python -m foundry run projects/hello \
  --input '{"name": "world"}' --stream
```

**Expected**:
- JSONL event lines appear PROGRESSIVELY (visibly before the run ends —
  the llm.* lines land seconds apart on a live key), in order:
  `run.started` → `agent.started` → `llm.started/…completed` →
  (`tool.started`/`tool.completed` if the model calls `get_time`) →
  `agent.completed` → `run.completed`.
- Every line carries the same `run_id`; `sequence` is 0,1,2,… gap-free.
- After the events: the pretty-printed `{"greeting": ...}` object; exit 0.

**If it fails**:
- Events all at once at the end → sink not synchronous; fix session.
- Missing lifecycle events → node wiring dropped an emitter call.

### Test 2 — Kill mid-run + resume with the same run id (THE gate)

**What we're verifying**: a killed process leaves a resumable SQLite
checkpoint; a new process with the same run id resumes and completes
without re-running completed work.

```bash
# 2a. Mint an id and start; Ctrl-C while the run is mid-flight
#     (easiest live window: memory_hello with several turns).
RUN_ID=$(uv run python -c "from foundry.core import RunId; print(RunId.new())")
uv run python -m foundry run projects/memory_hello --checkpoint sqlite \
  --run-id "$RUN_ID" --input '{"raw_turns": ["hi, my name is Sam",
  "I am planning a trip to Paris", "what should I not miss?",
  "remind me, what is my name?"]}'
# ^C during turn 2/3 (watch the log lines; llm.started marks a turn)

# 2b. Resume: SAME command, SAME --run-id
uv run python -m foundry run projects/memory_hello --checkpoint sqlite \
  --run-id "$RUN_ID" --input '{"raw_turns": ["hi, my name is Sam",
  "I am planning a trip to Paris", "what should I not miss?",
  "remind me, what is my name?"]}'
```

**Expected**:
- 2b exits 0 and prints the final reply (it still knows the name — state
  restored, not recomputed).
- `jq -r .event ~/.foundry/runs/$RUN_ID/events.jsonl` shows
  `run.started` twice, `agent.started` ONCE, `run.completed` once; the
  `sequence` values are continuous across both processes.
- Turns completed before the kill do NOT re-appear as new `llm.started`
  events after the second `run.started`.
- `jq '{status, resumed, checkpointer}' ~/.foundry/runs/$RUN_ID/metadata.json`
  → `completed / true / sqlite`.

**If it fails**:
- 2b starts from turn 1 → checkpoint not persisted or thread id mismatch.
- 2b errors on deserialization → serde regression; check the
  `FoundrySqliteSaver` hydration.

### Test 3 — Checkpoint file inspection

**What we're verifying**: the dev checkpointer is a plain, inspectable
SQLite file, per-project, under the foundry home.

```bash
ls ~/.foundry/checkpoints/
sqlite3 ~/.foundry/checkpoints/memory_hello.sqlite '.tables'
sqlite3 ~/.foundry/checkpoints/memory_hello.sqlite \
  "SELECT thread_id, count(*) FROM checkpoints GROUP BY thread_id;"
```

**Expected**: tables `blobs  checkpoints  writes`; one thread row per run
id used in Test 2, with several checkpoints (one per graph node boundary
— begin/turn/llm/turn_end/…). No secrets anywhere in the file:
`strings ~/.foundry/checkpoints/memory_hello.sqlite | grep -c "$ANTHROPIC_API_KEY"`
→ 0.

### Test 4 — `--checkpoint none` and completed-thread rerun

```bash
uv run python -m foundry run projects/hello --input '{"name": "world"}' \
  --checkpoint none
# rerun a COMPLETED id: fresh execution, not a stale replay
uv run python -m foundry run projects/hello --input '{"name": "resumed"}' \
  --checkpoint sqlite --run-id "$RUN_ID_FROM_A_COMPLETED_HELLO_RUN"
```

**Expected**: `none` run exits 0 and creates no
`~/.foundry/checkpoints/` entry for it; the completed-id rerun executes
fresh (new LLM call, greeting mentions "resumed") and its metadata says
`"resumed": false`.

### Test 5 — Phase 7 stubs stay honest

```bash
cp -r projects/hello /tmp/hello_parallel
python - <<'EOF'
from pathlib import Path
p = Path("/tmp/hello_parallel/system.yaml")
p.write_text(p.read_text().replace(
    "flow:\n  type: single\n  agent: hello_agent",
    "flow:\n  type: parallel\n  parallel_branches: [hello_agent, hello_agent]"))
EOF
uv run python -m foundry run /tmp/hello_parallel --input '{"name": "x"}'
```

**Expected**: exit 2, `CompileError` naming `parallel` and **Phase 7**,
no run artifact, no traceback.

### Test 6 — rag_hello + memory_hello regression through the graph path

```bash
uv run python -m foundry run projects/rag_hello \
  --input '{"query": "what is the capital of France?"}'
uv run python -m foundry run projects/memory_hello \
  --input '{"raw_turns": ["hi, my name is Sam", "remind me, what is my name?"]}'
```

**Expected**: both behave exactly as their Phase 2b/2c smoke tests
(retrieval + rerank + semantic cache events; function nodes + memory
events; `final_state.json` written). Exit 0.

## Sign-off

- [ ] Tests 1–6 pass on a live key.
- [ ] No `Traceback` printed anywhere; exit codes match.
- [ ] Operator notes any deviation in this file before Phase 4 starts.
