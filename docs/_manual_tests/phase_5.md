# Phase 5 — Manual Smoke Tests

**Phase scope**: `foundry.versioning` (git_backend, refs, artifacts, pins,
compat, rollback, audit) + `foundry.catalog.promote` + CLI (`rollback`,
`versions`, `diff`, `catalog promote`, `eval tool --project`).

**Reference**: [docs/03-development-phases.md § Phase 5](../03-development-phases.md)
exit gate; [docs/_phase_handoffs/phase_5.md](../_phase_handoffs/phase_5.md)
for deviations (sync GitBackend; branch check softens when
`foundry/<project>` doesn't exist; `no_inflight_runs` skipped in v1; audit
log gitignored, not git-tracked).

## Preconditions

- Phase 4 manual smoke test fully signed off.
- Claude Code review session for Phase 5 has reported **PASS**.
- Working tree clean; `uv run pytest tests/` green (569).
- No API keys needed for any test below.

## Setup

Rollback and promotion COMMIT — run the whole session in a scratch clone
so `main` is untouched, and throw it away afterwards:

```bash
git clone <repo-root> /tmp/foundry-smoke-5
cd /tmp/foundry-smoke-5
uv sync
audit() { tail -1 projects/hello/.foundry/audit.jsonl | python3 -m json.tool; }
```

## Tests

### Test 1 — Discovery: `foundry versions`

```bash
uv run python -m foundry versions projects/hello
uv run python -m foundry versions projects/hello --tool get_time
```

**Expect**: current branch + recent commits touching `projects/hello`;
agents with prompt versions and the ACTIVE pin starred (`v1, *v2`); tools
and connections with refs, on-disk versions, and "(vN available, not
pinned)" hints where applicable. `--tool` narrows to one line.

### Test 2 — Per-prompt rollback is a one-file commit

```bash
uv run python -m foundry rollback projects/hello --prompt hello_agent --to v1 --yes
git show --stat HEAD
git show HEAD -- projects/hello/agents/hello_agent/agent.yaml
audit
```

**Expect**: exit 0; plan + all-ok pre-flight printed; commit subject
`rollback(hello/agents/hello_agent): prompt v2 → v1`; the commit touches
ONLY `agent.yaml` (two changed lines: `prompt.version`, `prompt.path`);
audit line has a 26-char `id`, the commit sha, `type: rollback`,
`operator.kind: human` with YOUR git email, `overrides_used: []`.

### Test 3 — Dirty-tree refusal + `--force` override

```bash
echo "# scratch" >> projects/hello/state.yaml
uv run python -m foundry rollback projects/hello --prompt hello_agent --to v2 --yes; echo "exit=$?"
uv run python -m foundry rollback projects/hello --prompt hello_agent --to v2 --yes --force; echo "exit=$?"
audit
git checkout -- projects/hello/state.yaml
```

**Expect**: first attempt exit 1 with `working_tree_clean ... FAILED` and
NO commit; second exit 0; audit shows
`overrides_used: ["working_tree_clean"]`; your scratch edit is still in
the working tree (only the pin file was committed).

### Test 4 — `foundry diff` + dry-run

```bash
uv run python -m foundry diff projects/hello HEAD~1 HEAD
uv run python -m foundry diff projects/hello HEAD~1 HEAD --path evals/
uv run python -m foundry rollback projects/hello --to HEAD~2 --dry-run
git status --porcelain
```

**Expect**: the first diff shows the pin lines; the `--path evals/`
variant prints "(no differences ...)"; dry-run prints the plan (including
any files that WOULD be removed) and changes nothing — `git status` is
clean, no new commit, no new audit line.

### Test 5 — Per-project rollback is atomic

```bash
BASE=$(git rev-parse HEAD)
cp projects/hello/agents/hello_agent/prompts/v2.md projects/hello/agents/hello_agent/prompts/v3.md
git add . && git commit -m "smoke: extra prompt version"
uv run python -m foundry rollback projects/hello --to "$BASE" --yes
ls projects/hello/agents/hello_agent/prompts/
git status --porcelain
```

**Expect**: the plan warns `prompts/v3.md` will be REMOVED; after apply,
`v3.md` is gone, one `rollback(hello): bulk to ...` commit exists, tree is
clean. Bonus: re-running the same command exits 1 ("already identical").

### Test 6 — Catalog promotion end-to-end (+ floor + duplicate refusal)

```bash
mkdir -p projects/hello/tools/word_stats
cp -r catalog/tools/word_count/v1 projects/hello/tools/word_stats/v1
sed -i '' 's/^name: word_count/name: word_stats/' projects/hello/tools/word_stats/v1/tool.yaml
sed -i '' 's/word_count/word_stats/; s/catalog\//local\//' projects/hello/tools/word_stats/v1/eval.yaml
git add . && git commit -m "smoke: local word_stats"

uv run python -m foundry catalog promote hello/tool/word_stats --yes --notes "smoke"; echo "exit=$?"
cat catalog/tools/word_stats/versions.json
grep -n "word_stats" catalog/index.yaml
git show --stat HEAD
uv run python -m foundry eval tool catalog/word_stats@v1; echo "exit=$?"

# duplicate + floor refusals
uv run python -m foundry catalog promote hello/tool/word_stats --yes; echo "exit=$?"
uv run python -m foundry catalog promote hello/tool/word_stats --yes --floor 1.01; echo "exit=$?"
```

**Expect**: first promote exit 0 — the tool's standalone eval runs
(score 1.00 ≥ 0.85), `catalog/tools/word_stats/v1/` appears with all 5
files, `versions.json` records `eval_score`, `promoted_by` (your git
email), `source_ref`, `schema_change: initial`, `notes: smoke`;
`index.yaml` gained `- word_stats` with its comments intact; the commit
touches ONLY `catalog/` files; the promoted tool evals cleanly from the
catalog. Duplicate promote → exit 1 "content-identical". (The `--floor
1.01` variant proves the floor is configurable and blocking; note the
duplicate check fires first here, so read its message accordingly — for
a pure floor refusal, bump the local tool's eval to a failing expectation
and re-run.)

### Test 7 — Schema-incompatible rollback surfaces at next compile

```bash
uv run pytest tests/integration/test_rollback.py::test_incompatible_rollback_fails_next_compile -q
```

**Expect**: passes — this gate needs a two-version incompatible fixture
tool; the test builds it in a throwaway repo and asserts the rollback
succeeds while the NEXT `compile_project` fails naming the tool + the
unbound connection slot.

## Teardown

```bash
cd / && rm -rf /tmp/foundry-smoke-5
```

## Sign-off

- [ ] All 7 tests pass as described.
- [ ] The REAL repo (`<repo-root>`) shows no new
      commits and a clean tree afterwards.
- [ ] Operator notes any deviations in this file before Phase 6 starts.
