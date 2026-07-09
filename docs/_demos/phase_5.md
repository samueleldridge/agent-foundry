# Phase 5 demo — versioning you can trust: pins, rollback, promotion

No API keys needed — every command below is git + filesystem + (for
promotion) a pure tool eval. Run from the repo root. Because rollback
COMMITS, do the demo on a scratch branch (or a scratch clone) and delete
it after.

## Hero commands

```bash
git checkout -b demo/phase-5

# 1) Discovery: commits + per-artifact version state, active pins starred
uv run python -m foundry versions projects/hello

# 2) Per-prompt rollback: agent.yaml pin only, one commit, audit entry
uv run python -m foundry rollback projects/hello --prompt hello_agent --to v1 --yes
git show --stat HEAD               # exactly ONE file: agent.yaml
cat projects/hello/.foundry/audit.jsonl | tail -1 | python3 -m json.tool

# 3) Scoped diff of what the rollback did
uv run python -m foundry diff projects/hello HEAD~1 HEAD

# 4) Pre-flight refusal: dirty tree blocks, --force overrides (logged)
echo "# scratch" >> projects/hello/state.yaml
uv run python -m foundry rollback projects/hello --prompt hello_agent --to v2 --yes
# -> exit 1, "working_tree_clean ... FAILED"
uv run python -m foundry rollback projects/hello --prompt hello_agent --to v2 --yes --force
tail -1 projects/hello/.foundry/audit.jsonl | grep overrides_used
git checkout -- projects/hello/state.yaml

# 5) Dry-run: plan + pre-flight, zero changes
uv run python -m foundry rollback projects/hello --to HEAD~2 --dry-run

# 6) Roll the whole project back two commits (atomic), then inspect
uv run python -m foundry rollback projects/hello --to HEAD~2 --yes
git log --oneline -4

# clean up the demo branch
git checkout main && git branch -D demo/phase-5
```

## Promotion (throwaway sandbox)

Promotion writes into `catalog/` — demo it in a scratch clone so the real
catalog stays pristine:

```bash
git clone . /tmp/foundry-promo-demo && cd /tmp/foundry-promo-demo
mkdir -p projects/hello/tools/word_stats
cp -r catalog/tools/word_count/v1 projects/hello/tools/word_stats/v1
# make it a self-consistent local tool
sed -i '' 's/^name: word_count/name: word_stats/' projects/hello/tools/word_stats/v1/tool.yaml
sed -i '' 's/word_count/word_stats/; s/catalog\//local\//' projects/hello/tools/word_stats/v1/eval.yaml
git add . && git commit -m "demo: local word_stats"

uv run python -m foundry catalog promote hello/tool/word_stats --yes --notes "demo"
# -> "Promoted hello/tools/word_stats@v1 → catalog/word_stats@v1", eval 1.00
cat catalog/tools/word_stats/versions.json
uv run python -m foundry catalog promote hello/tool/word_stats --yes
# -> exit 1: content-identical to catalog word_stats@v1; nothing to promote
```

## Representative output

```
$ uv run python -m foundry rollback projects/hello --prompt hello_agent --to v1 --yes
Rollback (prompt) — project 'hello'
  agent 'hello_agent' prompt: v2 -> v1

Changes:
  projects/hello/agents/hello_agent/agent.yaml /prompt/version: v2 -> v1
  projects/hello/agents/hello_agent/agent.yaml /prompt/path: prompts/v2.md -> prompts/v1.md

Pre-flight checks:
  [ok] working_tree_clean: no uncommitted changes under the project
  [ok] correct_branch: branch foundry/hello does not exist; operating on demo/phase-5 (project lives on the default branch)
  [ok] no_inflight_runs: skipped — no run registry until Phase 8 (v1 deviation)
  [ok] target_exists: target prompt exists at .../prompts/v1.md

Applied. Commit: 3fa1b2c9
  rollback(hello/agents/hello_agent): prompt v2 → v1
Audit entry written (01JZW60M9GVJ2Q4Y8RDFH0K3T6).
  note: semantic cache entries for agent agent 'hello_agent' prompt are now unreachable (agent_version changed)
  v2 stays on disk; roll forward with --to v2.
```

## What this proves

- Rolling back ONE artifact touches ONE pin file — reviewable in a
  single `git show`.
- Pre-flight checks are mechanical and visible; every bypass is
  captured in the audit line (`overrides_used`).
- The audit log answers "what changed, who, which commit" without
  `git log` archaeology.
- Catalog versions only appear through the gated promote path — with an
  eval score attached to the version metadata forever.
