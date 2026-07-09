# Phase 6 — Manual Smoke Tests

**Phase scope**: `foundry.configurator` (meta-tools, MetaAgent, forge
session), `foundry.eval.failure_clustering`, `forge.*` events, CLI
(`project new`, `forge`).

**Reference**: [docs/03-development-phases.md § Phase 6](../03-development-phases.md)
exit gate; [docs/_phase_handoffs/phase_6.md](../_phase_handoffs/phase_6.md)
for deviations (autonomous mode only; violations terminate the forge;
frozen-once-superseded version immutability; no resume/interactive in v1).

## Preconditions

- Phase 5 manual smoke test fully signed off.
- Claude Code review session for Phase 6 has reported **PASS**.
- Working tree clean; `uv run pytest tests/` green (641).
- A real `ANTHROPIC_API_KEY` with a few dollars of headroom — this is
  the ONE checklist that exercises the meta-agent's actual reasoning.
  Expect roughly $1–5 for the full sheet at default settings.

## Setup

Forge COMMITS on a project branch — run everything in a scratch clone:

```bash
git clone /Users/sam/projects/agent-foundry /tmp/foundry-smoke-6
cd /tmp/foundry-smoke-6
uv sync
export ANTHROPIC_API_KEY=...   # never echo it
```

## 1. Project skeleton + branch

```bash
uv run python -m foundry project new qa_live
git branch --show-current           # -> foundry/qa_live
ls projects/qa_live                 # evals/ README.md — NO system.yaml
```

- [ ] Skeleton created on `foundry/qa_live`, committed, tree clean.
- [ ] Re-running `project new qa_live` refuses with exit 1.

## 2. The eval set (yours, not the meta-agent's)

Write `projects/qa_live/evals/qa.yaml` — the toy numeric-QA target
(copy the case shape from `tests/integration/forge_helpers.py`
`EVAL_SPEC_YAML`; 5–8 cases: `words:` / `digitsum:` questions, exact
scorer on `answer`, `threshold: 0.9`). Commit it.

- [ ] `git status` clean after the commit.

## 3. Live forge (the headline test)

```bash
uv run python -m foundry forge qa_live \
  --description "Answer numeric questions. 'words: <phrase>' -> the word
                 count; 'digitsum: <digits>' -> the sum of the digits.
                 Respond as JSON {\"answer\": \"<value>\"}. Prefer
                 catalog tools; build a local tool only if none fits." \
  --eval projects/qa_live/evals/qa.yaml \
  --threshold 0.9 --max-iter 5 --max-cost-usd 5
```

Watch the `[forge]` progress lines; the run may take several minutes.

- [ ] Forge terminates cleanly (threshold met, or an explicit best-effort
      / budget reason — a live model is allowed to be imperfect; what is
      NOT allowed is a crash or a corrupted project).
- [ ] `git log foundry/qa_live` shows one commit per iteration, subjects
      like `forge(qa_live/...)`, each body carrying
      `Iteration: <forge_run_id> | Eval: ... | Cluster: ...`.
- [ ] `system.yaml` pins at least one `catalog/...` tool.
- [ ] If a local tool was built: its `tools/<name>/v1/` has the 5-file
      shape and `~/.foundry/runs/` contains its standalone eval
      artifact(s).
- [ ] The printed summary matches
      `~/.foundry/runs/<forge_run_id>/final_summary.md`; that directory
      also holds `meta.json`, `trajectory.jsonl`, `events.jsonl`.
- [ ] `tail projects/qa_live/.foundry/audit.jsonl` — forge entries with
      `"kind": "meta_agent"` and your email as `human_supervisor`.
- [ ] `grep -r "$ANTHROPIC_API_KEY" ~/.foundry/runs/<forge_run_id>/`
      finds nothing (no secret leakage into artifacts).

## 4. The eval is the target

- [ ] `git log -- projects/qa_live/evals/` shows ONLY your commit — the
      meta-agent never touched the eval set.

## 5. Budget bite (cheap re-run)

```bash
uv run python -m foundry forge qa_live \
  --description "same as before" \
  --eval projects/qa_live/evals/qa.yaml \
  --threshold 0.99 --max-iter 2 --max-cost-usd 0.10
```

- [ ] Terminates with `cost_exhausted` or `max_iter` ("best effort") —
      never an unbounded run; exit code 1; trajectory artifact written.

## 6. Manual continuation (the human takes over)

- [ ] `uv run python -m foundry versions projects/qa_live` renders the
      forge commits + pins; `foundry rollback projects/qa_live --prompt
      <agent> --to v1 --dry-run` shows a sane plan against the
      meta-agent's history (don't apply).
- [ ] If the forged project passed: `uv run python -m foundry run
      projects/qa_live --input '{"question": "words: one two three"}'`
      answers correctly.

## 7. Teardown

```bash
cd / && rm -rf /tmp/foundry-smoke-6
```

- [ ] Scratch clone deleted; the real repo untouched.

## Sign-off

| Item | Operator | Date | Result |
|---|---|---|---|
| Sections 1–7 | | | |
