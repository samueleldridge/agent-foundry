# Phase 7 — Manual Smoke Tests

**Phase scope**: `foundry.orchestration` (patterns, predicates, handoff,
hitl), multi-agent runtime wiring, CLI (`resume`, `approvals list`),
`projects/team_hello`.

**Reference**: [docs/03-development-phases.md § Phase 7](../03-development-phases.md)
exit gate; [docs/_phase_handoffs/phase_7.md](../_phase_handoffs/phase_7.md)
for deviations (llm handoff mode only; tool-level approvals only; no
approval timeouts; graph is top-level; one pending approval per resume).

## Preconditions

- Phase 6 manual smoke test fully signed off.
- Claude Code review session for Phase 7 has reported **PASS**.
- Working tree clean; `uv run pytest tests/` green (722 + 1 skipped).
- A real `ANTHROPIC_API_KEY`. The sheet is cheap (~$0.10 at Haiku
  pricing — a dozen short calls).

## Setup

```bash
cd /Users/sam/projects/agent-foundry
uv sync
export ANTHROPIC_API_KEY=...   # never echo it
rm -f ~/.foundry/checkpoints/team_hello.sqlite   # fresh thread state
```

## 1. Supervisor + 2 workers, live (hero path → HITL pause)

```bash
uv run python -m foundry run projects/team_hello \
  --input '{"request": "the new release shipping", "audience": "the team"}' \
  --checkpoint sqlite --stream
```

Watch the JSONL stream, then the pause banner.

- [ ] `handoff` events appear: `coordinator→drafter` (trigger `llm`),
      `drafter→coordinator` (trigger `rule`), `coordinator→publisher`.
- [ ] An `approval.required` event fires with a `publish-<run_id>-...`
      approval id and the greeting text in the prompt.
- [ ] The process EXITS 0 with "run paused: approval required" + the
      run id + resume instructions. Note the RUN_ID.
- [ ] `run.completed` in the stream carries `status: approval_pending`.

## 2. The pending-approval surface

```bash
uv run python -m foundry approvals list
uv run python -m foundry resume <RUN_ID>
cat ~/.foundry/runs/<RUN_ID>/metadata.json
```

- [ ] `approvals list` shows one row: run id, `team_hello`, approval id,
      prompt prefix.
- [ ] `resume` (no flags) prints the full prompt + context + the
      resolve command.
- [ ] `metadata.json` has `status: approval_pending`, the payload, the
      absolute `project_path`, `checkpointer: sqlite`.

## 3. Approve — the run continues in a NEW process

```bash
uv run python -m foundry resume <RUN_ID> --approve
```

- [ ] Exits 0 printing the final JSON (`final_summary` mentions the
      greeting was published).
- [ ] `~/.foundry/runs/<RUN_ID>/final_state.json` shows
      `publish_status: "published"` and the drafter's `draft`.
- [ ] `events.jsonl`: exactly one `approval.required` and one
      `approval.resolved` (decision `approved`); sequence numbers are
      strictly increasing ACROSS the pause; the trail ends
      `handoff(publisher→coordinator)` → `handoff(coordinator→END)` →
      `run.completed(status: success)`.
- [ ] `approvals list` is empty again.

## 4. Reject — the reason reaches the agent

Fresh run (repeat step 1, new RUN_ID), then:

```bash
uv run python -m foundry resume <RUN_ID2> --reject --reason "tone is off"
```

- [ ] Exits 0; the final summary reflects a rejected publication.
- [ ] `final_state.json` → `publish_status` starts with `rejected` and
      contains "tone is off".
- [ ] `approval.resolved` event carries `decision: rejected`,
      `reason: "tone is off"`.
- [ ] `--reject` WITHOUT `--reason` exits 2 with a clear message.

## 5. Kill + resume in a multi-agent run

Start a fresh run (step 1 input) and Ctrl-C it while the stream shows
the drafter/coordinator working (before the pause). Then:

```bash
uv run python -m foundry run projects/team_hello \
  --input '{"request": "the new release shipping", "audience": "the team"}' \
  --checkpoint sqlite --run-id <RUN_ID3>
```

- [ ] The rerun RESUMES (does not restart): agents that already
      completed are not re-invoked (check `events.jsonl` — one
      `agent.started` for the drafter across both processes), and the
      run proceeds to the approval pause as normal.
- [ ] `resume --approve` on it completes the run.

## 6. Guardrails + compile-time gates (no key needed)

```bash
# Predicate sandbox refuses hostile config:
python3 - <<'EOF'
import subprocess, pathlib, shutil, tempfile
src = pathlib.Path("projects/team_hello")
tmp = pathlib.Path(tempfile.mkdtemp()) / "team_hello"
shutil.copytree(src, tmp)
system = tmp / "system.yaml"
system.write_text(system.read_text().replace(
    "  termination:\n    max_hops: 10\n    on_max_hops: error\n",
    '  termination:\n    when: "__import__(\'os\').system(\'id\')"\n'
    "    max_hops: 10\n    on_max_hops: error\n"))
print(subprocess.run(
    ["uv", "run", "python", "-m", "foundry", "run", str(tmp),
     "--input", "{}"], capture_output=True, text=True).stderr)
EOF
```

- [ ] Exit 2 with `CompileError` naming the forbidden construct +
      line/column — no code executed.
- [ ] `uv run pytest tests/integration/test_run_multi_agent_flows.py -q`
      passes locally (parallel concurrency barrier, graph routing,
      nesting, max_hops matrix, max_iterations).

## 7. Regression sweep

```bash
uv run pytest tests/ -q          # 722 passed, 1 skipped
uv run ruff check src/ tests/    # clean
uv run mypy --strict src/foundry # clean
uv run python -m foundry run projects/hello \
  --input '{"name": "world"}'    # Phase 1 hero still works live
```

- [ ] All green; single-agent hello unaffected.

## Sign-off

- [ ] All boxes checked → update operator memory + proceed to Phase 8.
- [ ] Anything failed → file it against Phase 7, do NOT start Phase 8.
