# Phase 0 — Manual Smoke Tests

**Phase scope**: repo skeleton — pinned deps, module tree, lint boundaries, importable empty modules, minimal CLI.

**Reference**: [docs/03-development-phases.md § Phase 0](../03-development-phases.md) exit gate; [docs/_phase_handoffs/phase_0.md](../_phase_handoffs/phase_0.md) for the implementing session's notes.

## Preconditions

- Claude Code review session for Phase 0 has reported **PASS**.
- You're on a clean working tree (`git status` clean; current branch is `main`).
- Phase 0 commit is at `HEAD` (check `git log -1`).

## Setup

No env vars or external credentials required for Phase 0 — there's no LLM call yet.

```bash
cd /Users/sam/projects/agent-foundry
```

## Tests

### Test 1 — Clean-clone bootstrap

**What we're verifying**: a first-time user can clone, run `uv sync`, and get a working environment. This catches hidden dependencies on your local `.venv` state.

**Run**:

```bash
rm -rf .venv
uv sync
```

**Expected**:
- Command exits 0.
- `.venv/` is recreated.
- No error about missing dependencies, version conflicts, or unresolved markers.
- `uv.lock` is unchanged after the run (`git diff uv.lock` is empty).

**If it fails**:
- `uv.lock` missing or stale → re-open Phase 0 implementation session and ask it to regenerate the lockfile.
- Version conflict → likely a bad pin in `pyproject.toml`; open a fresh fix-up session.

- [ ] Pass

### Test 2 — CLI help reads cleanly

**What we're verifying**: `python -m foundry --help` produces help text that a real user could understand, listing the planned subcommands per the doc.

**Run**:

```bash
uv run python -m foundry --help
```

**Expected**:
- Exit code 0.
- Output lists the planned subcommands (some may be stubs at Phase 0; that's fine). At minimum: `run`, `eval`, `forge`, `rollback`, `versions`, `catalog`, `serve`, `project`.
- No tracebacks, no "command not implemented" surprises beyond stubs.
- Reads coherently to a human; no obvious typos.

**If it fails**:
- Tracebacks → implementation issue, fresh fix session.
- Missing subcommand → check [docs/01-architecture-overview.md § CLI surface](../01-architecture-overview.md) to confirm what should be listed; may be a deliberate stub.

- [ ] Pass

### Test 3 — Lint passes

**What we're verifying**: `ruff check` is green on the committed code.

**Run**:

```bash
uv run ruff check src/ tests/
```

**Expected**: `All checks passed!` (or equivalent) and exit code 0.

**If it fails**: implementation drift since commit — open a fresh fix-up session pointing at the failures.

- [ ] Pass

### Test 4 — Import-boundary lint **actually** enforces boundaries (adversarial)

**What we're verifying**: the `ruff.toml` import-boundary rules don't just exist on paper — they fail when violated. This is the test the AI review session can't easily run.

**Run**:

```bash
# Inject a deliberately forbidden import into core/
echo "import langgraph  # deliberate violation" >> src/foundry/core/__init__.py
uv run ruff check src/foundry/core/ ; echo "exit=$?"
# Revert
git checkout -- src/foundry/core/__init__.py
```

**Expected**:
- Ruff reports the violation (cite TID253 or similar banned-API rule, or whichever rule the impl session chose).
- Exit code is non-zero.
- After the `git checkout`, `uv run ruff check src/` is clean again.

**Repeat for each forbidden module**: `langchain_core`, `langchain_anthropic`, `langchain_openai`, `foundry.runtime`, `foundry.config` — try each in turn, expect a violation each time.

**If it fails**:
- Lint stays silent on the bad import → `ruff.toml` boundary rules are misconfigured. Open a fresh fix-up session pointing at `docs/10-core-framework.md § Enforcement` for the correct rule set.
- Lint flags an unrelated rule but not the boundary → same fix.

- [ ] Pass

### Test 5 — Empty test suite runs

**What we're verifying**: `pytest` can discover and run the smoke test without import errors.

**Run**:

```bash
uv run pytest tests/ -v
```

**Expected**:
- Exit code 0.
- At least one test runs (the `test_smoke.py` that asserts `import foundry`).
- No collection errors, no missing-fixture errors.

**If it fails**: usually a missing `__init__.py` or a `conftest.py` config issue; small fix.

- [ ] Pass

### Test 6 — Directory layout matches spec (eyeball)

**What we're verifying**: the 18 `src/foundry/` subdirectories and the top-level `catalog/` + `projects/` exist with the right shape.

**Run**:

```bash
tree -L 2 -d src/foundry/ ; echo "---" ; ls catalog/ projects/
```

**Expected**: every subdirectory listed in [docs/01-architecture-overview.md § Directory layout](../01-architecture-overview.md#directory-layout-target-shape-of-the-repo) is present. `catalog/` and `projects/` each contain a `.gitkeep` + `README.md`.

**Cross-check** by reading the architecture doc's Directory layout section side-by-side with the `tree` output. Missing dirs are easy to spot.

**If it fails**: missing subdir → fresh fix-up session referencing the spec section.

- [ ] Pass

### Test 7 — Pinned dependency rationale documented

**What we're verifying**: the chosen pins for `langgraph`, `langchain-core`, `pydantic`, and the OTel packages are recorded somewhere a future operator (or you in 3 months) can reconstruct the decision.

**Look at**: `pyproject.toml` (chosen pin + comment explaining version choice) AND [docs/_phase_handoffs/phase_0.md](../_phase_handoffs/phase_0.md) (rationale block).

**Expected**: for each constrained dep, the handoff note explains *why* this version (e.g., "langgraph 0.x.y — latest stable as of <date>; pinned exact because checkpointer API changes between minors").

**If it fails**: ask the implementation session to add the rationale before declaring Phase 0 done.

- [ ] Pass

### Test 8 — Commit hygiene

**What we're verifying**: the Phase 0 commit follows conventional format, has no Claude co-author line, and has no firm-name or institutional leakage.

**Run**:

```bash
git log -1 --format="%H%n---%n%s%n%n%b"
```

**Expected**:
- Subject line uses a conventional prefix (`feat(phase-0):`, `chore:`, etc.).
- **No** `Co-Authored-By: Claude` line in the body.
- No mentions of specific firms, internal systems, or client names.

**If it fails**: amend the commit (`git commit --amend`) to fix the message; or, if the diff itself contains leakage, fresh fix session.

- [ ] Pass

## Sign-off

When every box above is ticked:

- [ ] All 8 tests passed.
- [ ] No surprises in output worth recording for the retro.
- [ ] Ready to start Phase 1.

Signed off: ____________________ Date: __________

If anything was non-obvious or worth remembering, add a one-line note to `docs/_retros/phase_0.md` before moving on.
