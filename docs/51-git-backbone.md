# 51 — Git Backbone

## Purpose

Git is the foundry's versioning substrate. Every meta-agent change, every human edit, every pin bump becomes a git commit on a project-specific branch. This is what makes rollback trustworthy (`52-rollback-and-audit.md`), what makes the audit trail real, and what makes the meta-agent's iteration loop reviewable.

This doc specifies: why git (vs alternatives), the branch model, commit message conventions, atomic multi-file operations, the meta-agent's git operations + sandbox, the wrapping strategy (subprocess vs Python git library), pre-commit hooks (secret scan), conflict handling, and CI integration.

The versioning model itself is in `50-versioning-model.md`. Rollback semantics are in `52-rollback-and-audit.md`. This doc is the implementation spec for the git layer underneath.

Three load-bearing properties:

1. **Git is the only version-control system the foundry uses.** No SVN adapter, no VCS-less mode, no custom storage. The meta-agent shells out to `git` (subprocess); other code may use a Python library but the meta-agent's tools are subprocess for predictability.
2. **Per-project branches.** Every foundry project gets a branch `foundry/<project_name>`. Meta-agent commits land there; humans review there; rollback operates against that branch. This is independent of any other git workflow the institution uses (a project's foundry branch can merge into a release branch separately).
3. **Atomic multi-file commits.** When a change touches multiple files (a pin bump in `system.yaml` + a new prompt in `prompts/`), they land in one commit. Either the whole change is committed or none of it is. Rollback works the same way.

## Why git (vs alternatives)

Considered options:

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| **Git** (chosen) | Universal; rich diff/log/revert tooling; meta-agent can shell out predictably; integrates with the institution's existing review workflow (PRs, blame, hooks) | Some operations have edge cases (force-push, rebase, conflict resolution); learning curve for git-novice operators | ✅ |
| SVN-like centralised VCS | Simpler conflict model; centralised authority | Less flexibility; smaller ecosystem; meta-agent operations less predictable | ❌ |
| Custom SQLite content store | Full programmatic control; no shell-out needed | Rebuilds half of git poorly; loses ecosystem (review tools, IDE integration); operator can't `git log` | ❌ |
| VCS-less filesystem versioning (`v1.md`, `v2.md` only) | Simple | Loses cross-file diff history (e.g., changes to `system.yaml` over time); no rollback for git-versioned axis | ❌ — covers only one axis of the three-axis model |

Git wins on every dimension that matters for a tool that has to be honest with operators about what changed.

## Branch model

### Per-project branches

Every project has a dedicated branch:

```
foundry/<project_name>
```

Examples: `foundry/pipeline_recon`, `foundry/contract_review`.

Created on `foundry project new <name>`; never deleted by the foundry (operators may delete manually if archiving).

### What lives on the project branch

Only changes scoped to `projects/<project_name>/`. The meta-agent's sandbox prevents cross-project writes; commits on `foundry/pipeline_recon` touch only `projects/pipeline_recon/**`.

Catalog changes (`catalog/**`) live on `main` (or whatever the institution chose as the catalog branch). Catalog promotions are NOT meta-agent operations — they're human-initiated, reviewed via PR.

### Relationship to `main`

- `main` is whatever the institution chose; conventionally the catalog + framework-pinning branch.
- Project branches are based off `main` at project creation; they may rebase onto a newer `main` (to pick up catalog updates) but the meta-agent never does this — humans drive rebases.
- Merging a project branch back into `main` is uncommon (project artifacts are project-private; they don't graduate to `main`). Catalog promotions ARE the path from project to shared.

### Multi-project repos

A single repo hosts multiple project branches. `foundry serve --project a --project b` runs from the same checkout (could be a worktree per branch, or a single working tree with branch switching for ops; the foundry doesn't mandate either).

For multi-institution deployments (`86-multi-tenancy-and-ip.md`), each institution has its own private repo with its own `main` and its own project branches. No cross-institution branch visibility.

## Commit message conventions

The meta-agent writes structured commits per a fixed format. Humans following the convention is recommended but not enforced.

### Format

```
<type>(<scope>): <short summary>

<body>

Iteration: <forge_run_id> | Eval: <prev_score> → <current_score> | Cluster: <cluster_id>
```

Where:

- `<type>`: one of `forge`, `human`, `rollback`, `pin`, `catalog`. (Distinct from conventional commits' `feat`/`fix`/etc. — these are foundry-internal types optimised for audit queries.)
- `<scope>`: usually `<project_name>/<artifact>` (e.g. `pipeline_recon/agents/investigator`).
- `<short summary>`: 50 chars or less, imperative voice.
- `<body>`: optional; longer explanation if the change is non-obvious.
- The trailer line is mandatory for meta-agent commits: ties the commit to a forge run + records eval movement + identifies the failure cluster being targeted.

Example meta-agent commit:

```
forge(pipeline_recon/agents/investigator): prompt v3 → v4

Strengthened guidance on amendment-timestamp checks. Added explicit
example for partial-fill vs rounding distinction.

Iteration: 01JKM4ABCDEF | Eval: 0.82 → 0.89 | Cluster: late_amendment
```

Example human commit (recommended format):

```
human(pipeline_recon/system.yaml): pin query_snowflake v2 → v3

Manual upgrade after standalone eval showed v3 is faster on FX queries
without correctness regression.
```

Example rollback commit:

```
rollback(pipeline_recon/system.yaml): pin validate_deltas v3 → v2

Eval comparison shows v3 regressed on partial_settlement cluster
(0.92 → 0.71). Returning to v2 pending v4 fix.
```

### Why this format

- `<type>` enables fast audit queries: "show me all forge iterations on this project last quarter" (`git log --grep '^forge(pipeline_recon'`).
- `<scope>` tells you what was touched without reading the diff.
- The trailer makes meta-agent activity tied to its forge run + the eval signal that drove the change. Audit completeness depends on this.

### Validation

Pre-commit hook (optional) validates the format on commit. The meta-agent's `git_commit` tool always produces a valid format by construction.

## Atomic multi-file commits

A pin bump that adds a new prompt + edits the agent.yaml's prompt pin is two file changes that must commit together (otherwise an in-flight read could see the new pin but missing prompt, or vice versa).

The meta-agent's `git_commit` tool takes a list of file paths + a message; the corresponding files are staged AND committed in one operation. There's no separate "stage" / "commit" split exposed.

```python
git_commit(
    files=[
        "projects/pipeline_recon/agents/investigator/prompts/v4.md",
        "projects/pipeline_recon/agents/investigator/agent.yaml",
    ],
    message="forge(...): ..."
)
```

The implementation:

```bash
cd <repo_root>
git add projects/pipeline_recon/agents/investigator/prompts/v4.md \
        projects/pipeline_recon/agents/investigator/agent.yaml
git commit --no-edit -m "..."
```

Failure modes:
- One file doesn't exist → `git add` fails; nothing staged; nothing committed; tool returns error.
- Pre-commit hook fails (secret scan, schema validation) → commit refused; staged files remain staged for inspection but no commit produced.
- Working tree had unrelated changes → those stay unstaged; only the explicitly-listed files are touched.

## The meta-agent's git operations (sandbox)

The meta-agent has a narrow set of git operations available as meta-tools (per `61-meta-tools.md`). The sandbox is what makes letting an LLM commit to a real repo trustworthy.

### Allowed operations

| Tool | What it does | Sandbox check |
|---|---|---|
| `git_commit(files, message)` | Stage + commit the listed files with the message | files MUST be inside `projects/<scoped_project>/` |
| `git_show(commit)` | Show the diff for a commit | commit MUST be on the scoped project's branch |
| `git_log(limit)` | Show recent commits on the project's branch | scoped to `foundry/<scoped_project>` only |
| `git_diff(ref1, ref2, paths)` | Diff between refs scoped to paths | paths MUST be inside the scoped project |
| `list_versions()` | Enumerate versioned artifacts (a foundry abstraction over `git log`) | scoped |

### Forbidden operations (meta-agent CANNOT call)

- `git push` (humans push)
- `git pull` / `git fetch`
- `git rebase` / `git reset --hard` / `git reflog`
- `git checkout <branch>` (the meta-agent operates on its scoped branch; switching is a human op)
- `git merge`
- `git tag` (catalog promotion uses tags; promotion is human-gated)
- `git config` (NEVER touched per Tier 0 rule)
- `--force` / `--force-with-lease` on anything

### Branch sandbox

The meta-agent's tools verify (before every operation) that the current branch is `foundry/<scoped_project>`. If not (someone left the working tree on `main`, say), the tool refuses with a clear error: "expected branch foundry/pipeline_recon, found main; checkout the branch before invoking forge."

The CLI's `foundry forge <project>` ensures the branch is correct before invoking the meta-agent — it's an operator-side guard, not just runtime.

## Subprocess wrapping (vs Python git library)

The meta-agent's git tools shell out via `subprocess` to `git` directly. The choice is deliberate:

| Subprocess | Python library (e.g. dulwich, GitPython) |
|---|---|
| Same behaviour as the user's `git` CLI | Library may diverge from current git behaviour |
| Predictable error output (stderr captured) | Library exceptions to learn |
| No dependency on a maintained git library | Keeps deps lean |
| Easy to reason about: "what did the meta-agent run?" | More layers of abstraction |

Cost: subprocess overhead (~10ms per `git` call). Acceptable; meta-agent isn't in the hot path.

Implementation in `foundry.versioning.git_backend`:

```python
class GitBackend:
    def __init__(self, repo_root: Path):
        self.repo_root = repo_root

    async def run_git(self, *args: str, check: bool = True) -> str:
        """Subprocess git with timeout, cancellation, structured error."""
        process = await anyio.run_process(
            ["git"] + list(args),
            cwd=self.repo_root,
            check=False,
            stdout=PIPE,
            stderr=PIPE,
        )
        if check and process.returncode != 0:
            raise GitBackendError(
                f"git {' '.join(args)} failed: {process.stderr.decode()}",
                context={"returncode": process.returncode, "stdout": process.stdout.decode()},
            )
        return process.stdout.decode()
```

Each git operation is a thin wrapper:

```python
async def commit(self, files: list[Path], message: str) -> str:
    """Stage files + commit. Returns commit sha."""
    rel_files = [f.relative_to(self.repo_root) for f in files]
    await self.run_git("add", "--", *map(str, rel_files))
    await self.run_git("commit", "-m", message)
    sha = await self.run_git("rev-parse", "HEAD")
    return sha.strip()
```

Other foundry modules (versioning helpers, audit queries) MAY use a Python git library if it materially helps — the constraint is specifically on the meta-agent's tool surface for predictability.

## Pre-commit hooks

The foundry installs a pre-commit hook on `foundry init` that:

1. **Secret-literal scan**: same scan as the config loader (per `12-config-and-validation.md` § Secrets). Refuses commits containing AWS keys, Anthropic / OpenAI key prefixes, or generic password-shaped values. Pragma comments (`# foundry:allow-literal`) opt out per line.

2. **Schema validation**: any `.yaml` file under `projects/<*>/` or `catalog/` validates via Pydantic before commit. Catches typo'd fields locally instead of at deploy time.

3. **Conventional message validation**: the commit message matches the `<type>(<scope>): <summary>` format. Warning by default; `--strict` mode (configurable in `~/.foundry/config.yaml`) makes it an error.

4. **Eval gate** (optional, off by default): on commits to a project branch, run a fast subset of the project eval. Refuses commit if score drops below configured floor. Useful for institutions that want belt-and-braces; off by default because pre-commit slowness pushes operators to skip hooks.

The hook lives at `.git/hooks/pre-commit` after `foundry init`. Standard git semantics — `git commit --no-verify` skips it (escape hatch for genuine emergencies; logged loudly to audit).

## Conflict handling

In personal-tool / single-operator workflows, conflicts on `foundry/<project>` are rare — only one workflow writes to that branch. They can happen if:

- A human edited a file the meta-agent is about to write (working tree has uncommitted changes the meta-agent didn't expect).
- Two `forge` invocations run concurrently on the same project (operator error).
- A `git checkout`/`reset` was done out-of-band, leaving the branch in an unexpected state.

The meta-agent's `git_commit` tool's response on detecting a conflict-shaped error:

```
GitBackendError: failed to commit:
  refusing to overwrite uncommitted changes to:
    projects/pipeline_recon/system.yaml
  
Resolution: commit or stash your changes, then re-invoke forge.
```

The meta-agent does NOT attempt automatic conflict resolution. Humans resolve; meta-agent re-attempts when the working tree is clean.

For multi-operator workflows (institution with 3 engineers all using `foundry forge` against the same project), the recommendation is one operator per project per session. Locking primitive (`foundry project lock <name>`) is a Phase 9 polish item; deferred for v1.

## CI integration

A typical CI pipeline for an institution repo:

```yaml
# .github/workflows/foundry-ci.yml (example)
on: [push, pull_request]
jobs:
  validate:
    steps:
      - run: foundry validate            # all configs load cleanly
      - run: foundry doctor              # runtime checks (catalog roots, sandbox, etc.)
      - run: foundry connections health  # all connections healthy against test creds
      - run: foundry eval --fail-under 0.90 \
               projects/pipeline_recon \
               projects/pipeline_recon/evals/q1.yaml
```

Exit codes are stable (per `40-eval-harness.md`):
- 0 — pass
- 1 — eval below threshold
- 2 — infrastructure failure

CI gates merges to `main` (or whatever release branch the institution uses) on these checks passing. For project branches, CI may run partial subsets; for `main` merges, CI runs the full eval suite.

The meta-agent does NOT push to CI directly; humans push, CI runs against pushed branches.

## Hook + tag use beyond the foundry's own ops

The foundry uses git tags only for:

- Catalog versions: `catalog/<artifact>@v<N>` tagged on the commit that created the version. Used for release notes + audit cross-referencing.
- Framework releases: `foundry-1.3.0` tagged on `main` of the upstream framework repo.

Project-level tags (release tags, deployment markers) are institution conventions; the foundry doesn't mandate them.

Hook customisation: institutions may add their own pre-commit hooks alongside the foundry's. The foundry's hook chains to user hooks if both exist (foundry hook runs first; user hook runs after; failure of either fails the commit).

## Composition with the audit log

Every commit on a project branch produces a corresponding entry in `projects/<name>/.foundry/audit.jsonl` (per `52-rollback-and-audit.md`). The audit entry duplicates some metadata that's also in the git commit (sha, timestamp, message) but adds:

- `forge_run_id` (if applicable).
- `eval_score_before`, `eval_score_after`.
- `cluster_id` targeted (if applicable).
- `operator` (resolved from auth context or git config; falls back to `unknown`).
- `artifact_affected` (canonical ref form).

Why duplicate? Audit queries (`foundry obs ...`) hit the JSONL file directly without shelling out to `git log`; substantially faster for large histories. Git is the source of truth for content; audit log is the source of truth for queryability.

## Failure modes

| Cause | Surfaced as | Recovery |
|---|---|---|
| `git` not installed | `GitBackendError("git binary not found in PATH")` at startup | install git |
| Working tree has uncommitted changes the meta-agent wasn't expecting | `GitBackendError("dirty working tree")` | commit / stash / `git restore` |
| Wrong branch checked out | `GitBackendError("expected foundry/<project>, found <X>")` | `git checkout foundry/<project>` |
| Secret detected by pre-commit hook | hook exit non-zero; commit refused | remove secret OR `# foundry:allow-literal` pragma |
| Schema validation in pre-commit fails | hook exit non-zero | fix the YAML |
| Eval gate fails | hook exit non-zero | iterate or override with `--no-verify` (logged) |
| Concurrent `forge` invocations on same project | second invocation's commit conflicts with first | second operator restarts after first completes |
| Catalog write attempt by meta-agent | `GitBackendError("catalog writes are human-gated")` | use `foundry catalog promote` |
| Forbidden git op attempted (force, push, rebase) | `GitBackendError` at the meta-tool layer (before subprocess runs) | not applicable — guard rejects |

Every git failure raises a `GitBackendError` (typed under `VersioningError` in the exception hierarchy) with `context` containing the failing command + stderr. The audit trail captures these too — failed commits are auditable.

## Invariants

1. **The meta-agent never invokes `git` directly via shell.** It calls `git_commit` / `git_show` / etc. tools that wrap and sandbox.
2. **Forbidden git operations are rejected at the meta-tool layer**, not just at the git binary level. Defence in depth.
3. **Project commits are scoped.** A commit on `foundry/pipeline_recon` may only touch `projects/pipeline_recon/**`. Hook validates.
4. **Conventional message format** is recommended; meta-agent always conforms.
5. **Pre-commit hook honours `--no-verify` only with explicit operator intent.** When invoked, the override is recorded in the audit log.
6. **Atomic commits across files** — partial commits not exposed as a meta-tool surface.
7. **The current branch matches the project being operated on.** Meta-agent guards check before every git operation.

## Test expectations

### Unit

1. **`run_git` subprocess wrapper**: success returns stdout; failure raises `GitBackendError` with stderr + returncode in context.
2. **Atomic commit**: multi-file commit succeeds; one file missing → entire commit aborts; nothing staged left over.
3. **Branch sandbox**: meta-tool refuses if current branch ≠ `foundry/<scoped_project>`.
4. **Forbidden operations**: each forbidden op (push, rebase, force, config, merge) raises `GitBackendError` at the meta-tool layer without invoking subprocess.
5. **Path scoping**: meta-tool refuses if any file path is outside `projects/<scoped_project>/`.
6. **Pre-commit hook secret detection**: a commit with an Anthropic-keyed string in YAML is refused; with `# foundry:allow-literal` pragma above the line, accepted.
7. **Pre-commit hook schema validation**: an invalid YAML field refuses the commit.
8. **Commit message format validation**: meta-agent commit always conforms to `<type>(<scope>): ...`.

### Contract

1. **No `git` from meta-agent module**: lint check `grep -rn "subprocess.*git" src/foundry/configurator/` returns zero hits (configurator only goes via `foundry.versioning.git_backend`).
2. **No catalog writes from meta-agent**: meta-agent's sandbox refuses writes to `catalog/**`.

### Integration (Phase 5 exit gate)

1. End-to-end: meta-agent commits a prompt change atomically (prompt file + agent.yaml pin); `git log` shows the commit with conventional format; audit log entry matches.
2. Cross-process consistency: `foundry forge` invocation in process A commits; process B (same repo) sees the commit immediately on its next `git log`.
3. Pre-commit secret scan: deliberate AWS-key-shaped value in a yaml → commit refused with clear message.

## Open questions

1. **Per-project locking** — to prevent two concurrent `forge` invocations on the same project. Lean: yes, simple file-based lock at `.foundry/locks/<project_name>.lock` with PID + heartbeat. Phase 9 polish.
2. **Sub-modules / sub-trees**. Some institutions might want their catalog to be a git submodule of a shared catalog repo. Lean: not built-in v1; document the pattern as "set FOUNDRY_CATALOG_ROOTS to your submodule path." Multi-institution shared catalog is the v1.1 use case (per `86-multi-tenancy-and-ip.md`).
3. **Branch-naming convention configurability**. Currently `foundry/<project>` is hardcoded. Some institutions might want `proj/<project>` or similar. Lean: yes, configurable via `~/.foundry/config.yaml`; default stays `foundry/<project>`.
4. **Auto-rebase project branches onto main**. When `main` advances (catalog updates), project branches lag. Should the meta-agent rebase automatically? Lean: NO — rebase is a humans-only operation; introduces conflict-resolution complexity meta-agent shouldn't handle.
5. **Long-lived feature branches off project branches**. Operator-driven workflows where a multi-day exploration branches off `foundry/pipeline_recon` to `foundry/pipeline_recon/experiment-fx`, eventually merged back. Today this works (it's just git) but the foundry's tools assume the project branch is the working branch. Document the pattern; the meta-agent can be invoked against the experiment branch by setting an env var (`FOUNDRY_PROJECT_BRANCH=foundry/pipeline_recon/experiment-fx`).
