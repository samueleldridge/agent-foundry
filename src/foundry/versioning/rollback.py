"""Per-tool, per-prompt, per-project rollback (docs/52).

Three granularities, one shape: ``plan_*`` builds a :class:`RollbackPlan`
(what will change + the mandatory pre-flight checks, docs/52 § Rollback
safety guards), ``execute_rollback`` applies it atomically — pin edit (or
subtree restore) + ONE commit + audit entry — or applies nothing.

Pre-flight checks and their bypass rules:

===================  =========================================  ==========
check                what it verifies                            bypass
===================  =========================================  ==========
working_tree_clean   no uncommitted changes under the project    ``--force``
correct_branch       ``foundry/<project>`` checked out when      none (hard)
                     that branch exists (repos whose projects
                     live on the default branch — e.g. the
                     bundled examples — pass with a note)
target_exists        version dir / prompt file / commit exists   none (hard)
no_inflight_runs     recorded as SKIPPED in v1 — there is no     n/a
                     run registry until Phase 8 (deviation
                     noted in the Phase 5 handoff)
schema_compatible    (per-tool only) target version's contract   confirm or
                     vs the pinned one (docs/50 § evolution)     ``--force``
===================  =========================================  ==========

Every bypass lands in the audit entry's ``overrides_used`` (docs/52).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from foundry.core.errors import RollbackError
from foundry.core.types import RunId
from foundry.observability.logging import run_logger
from foundry.observability.tracing import foundry_span
from foundry.versioning.audit import (
    AuditEntry,
    Operator,
    append_audit_entry,
    new_audit_entry,
    resolve_operator,
)
from foundry.versioning.compat import ContractDiff, tool_contract_diff
from foundry.versioning.git_backend import GitBackend
from foundry.versioning.pins import (
    PinTransaction,
    read_prompt_pin,
    read_tool_pin,
)
from foundry.versioning.refs import parse_artifact_ref

Granularity = Literal["tool", "prompt", "project"]

Bypass = Literal["none", "force", "confirm"]

_PROJECT_BRANCH_ENV = "FOUNDRY_PROJECT_BRANCH"


@dataclass(frozen=True)
class PreflightCheck:
    name: str
    ok: bool
    detail: str
    bypass: Bypass = "none"
    """How a failed check may be bypassed: 'force' (--force), 'confirm'
    (interactive yes / --yes / --force), 'none' (hard failure)."""

    def render(self) -> str:
        mark = "ok" if self.ok else "FAILED"
        return f"  [{mark}] {self.name}: {self.detail}"


@dataclass(frozen=True)
class RollbackPlan:
    granularity: Granularity
    project_name: str
    project_dir: Path
    artifact: str
    """What is being rolled back: tool name, agent name, or the project."""
    current: str
    target: str
    commit_message: str
    changes: list[str]
    """Human-readable planned changes."""
    files: list[str]
    """Repo-relative paths the rollback commit will touch."""
    removed_files: list[str] = field(default_factory=list)
    """Project mode: files added since the target commit → will be DELETED."""
    checks: list[PreflightCheck] = field(default_factory=list)
    txn: PinTransaction | None = None

    def render(self) -> str:
        lines = [
            f"Rollback ({self.granularity}) — project {self.project_name!r}",
            f"  {self.artifact}: {self.current} -> {self.target}",
            "",
            "Changes:",
            *[f"  {c}" for c in self.changes],
        ]
        if self.removed_files:
            lines += [
                "",
                "Files added since the target (will be REMOVED — you lose "
                "the ability to roll forward to them from the new HEAD):",
                *[f"  {f}" for f in self.removed_files],
            ]
        lines += ["", "Pre-flight checks:", *[c.render() for c in self.checks]]
        return "\n".join(lines)


@dataclass(frozen=True)
class RollbackResult:
    commit_sha: str
    audit_entry: AuditEntry
    overrides_used: list[str]
    notes: list[str] = field(default_factory=list)
    """Operator-facing acknowledgements (e.g. cache-invalidation effects)."""


# --- shared pre-flight pieces --------------------------------------------------------


def _common_checks(
    backend: GitBackend, project_dir: Path, project_name: str
) -> list[PreflightCheck]:
    rel = backend.relpath(project_dir)
    dirty = backend.is_dirty(paths=[rel])
    checks = [
        PreflightCheck(
            name="working_tree_clean",
            ok=not dirty,
            detail=(
                "no uncommitted changes under the project"
                if not dirty
                else f"uncommitted changes under {rel} — commit or stash "
                "first (or --force, logged to audit)"
            ),
            bypass="force",
        )
    ]
    expected = os.environ.get(_PROJECT_BRANCH_ENV) or f"foundry/{project_name}"
    current = backend.current_branch()
    if backend.branch_exists(expected):
        ok = current == expected
        detail = (
            f"on branch {expected}"
            if ok
            else f"expected branch {expected}, found {current}; "
            f"`git checkout {expected}` first (not force-able, docs/52)"
        )
    else:
        ok = True
        detail = (
            f"branch {expected} does not exist; operating on {current} "
            "(project lives on the default branch)"
        )
    checks.append(
        PreflightCheck(name="correct_branch", ok=ok, detail=detail, bypass="none")
    )
    checks.append(
        PreflightCheck(
            name="no_inflight_runs",
            ok=True,
            detail="skipped — no run registry until Phase 8 (v1 deviation)",
            bypass="none",
        )
    )
    return checks


def _require_pin(current: str, target: str, *, what: str) -> None:
    if current == target:
        raise RollbackError(
            f"{what} is already pinned at {target}; nothing to roll back",
            context={"current": current, "target": target},
        )


# --- planners ---------------------------------------------------------------------------


def plan_tool_rollback(
    project_dir: Path, tool: str, target_version: str, *, backend: GitBackend
) -> RollbackPlan:
    """Per-tool rollback: ONE pin line in system.yaml (docs/52 granularity 1)."""
    project_dir = project_dir.resolve()
    project_name = project_dir.name
    ref_str, current_version = read_tool_pin(project_dir, tool)
    _require_pin(current_version, target_version, what=f"tool {tool!r}")

    from foundry.config.refs import FoundryRoots

    roots = FoundryRoots.for_project(project_dir)
    ref = parse_artifact_ref(ref_str, version=target_version)
    checks = _common_checks(backend, project_dir, project_name)

    target_dir: Path | None = None
    try:
        target_dir = ref.resolve_path(roots)
        checks.append(
            PreflightCheck(
                name="target_exists",
                ok=True,
                detail=f"target version {target_version} exists at {target_dir}",
            )
        )
    except Exception as exc:  # RefResolutionError carries available versions
        checks.append(
            PreflightCheck(name="target_exists", ok=False, detail=str(exc))
        )

    if target_dir is not None:
        current_ref = parse_artifact_ref(ref_str, version=current_version)
        try:
            current_dir = current_ref.resolve_path(roots)
            diff = tool_contract_diff(current_dir, target_dir)
            checks.append(_schema_check(diff, current_version, target_version))
        except Exception as exc:
            checks.append(
                PreflightCheck(
                    name="schema_compatible",
                    ok=False,
                    detail=f"could not compare contracts: {exc}",
                    bypass="confirm",
                )
            )

    txn = PinTransaction(project_dir)
    change = txn.set_tool_version(tool, target_version)
    system_rel = backend.relpath(project_dir / "system.yaml")
    return RollbackPlan(
        granularity="tool",
        project_name=project_name,
        project_dir=project_dir,
        artifact=f"tool {tool!r} ({ref_str})",
        current=current_version,
        target=target_version,
        commit_message=(
            f"rollback({project_name}/system.yaml): pin {tool} "
            f"{current_version} → {target_version}"
        ),
        changes=[f"{system_rel} {change.describe()}"],
        files=[system_rel],
        checks=checks,
        txn=txn,
    )


def _schema_check(
    diff: ContractDiff, current: str, target: str
) -> PreflightCheck:
    if not diff.breaking:
        return PreflightCheck(
            name="schema_compatible",
            ok=True,
            detail=f"{target} contract is compatible with {current}'s",
        )
    return PreflightCheck(
        name="schema_compatible",
        ok=False,
        detail=(
            f"{target} is contract-INCOMPATIBLE with {current}: "
            + "; ".join(diff.breaking)
            + " — the rollback can proceed (confirm / --force) but the next "
            "run will surface this at compile time"
        ),
        bypass="confirm",
    )


def plan_prompt_rollback(
    project_dir: Path, agent: str, target_version: str, *, backend: GitBackend
) -> RollbackPlan:
    """Per-prompt rollback: the PromptRef pin in agent.yaml only."""
    project_dir = project_dir.resolve()
    project_name = project_dir.name
    current_version, _current_path = read_prompt_pin(project_dir, agent)
    _require_pin(
        current_version, target_version, what=f"agent {agent!r} prompt"
    )
    checks = _common_checks(backend, project_dir, project_name)

    target_file = (
        project_dir / "agents" / agent / "prompts" / f"{target_version}.md"
    )
    checks.append(
        PreflightCheck(
            name="target_exists",
            ok=target_file.is_file(),
            detail=(
                f"target prompt exists at {target_file}"
                if target_file.is_file()
                else f"no prompt file at {target_file}"
            ),
        )
    )

    txn = PinTransaction(project_dir)
    changes = txn.set_prompt_version(agent, target_version)
    agent_rel = backend.relpath(project_dir / "agents" / agent / "agent.yaml")
    return RollbackPlan(
        granularity="prompt",
        project_name=project_name,
        project_dir=project_dir,
        artifact=f"agent {agent!r} prompt",
        current=current_version,
        target=target_version,
        commit_message=(
            f"rollback({project_name}/agents/{agent}): prompt "
            f"{current_version} → {target_version}"
        ),
        changes=[f"{agent_rel} {c.describe()}" for c in changes],
        files=[agent_rel],
        checks=checks,
        txn=txn,
    )


def plan_project_rollback(
    project_dir: Path, target_commit: str, *, backend: GitBackend
) -> RollbackPlan:
    """Per-project (coarse) rollback: restore the whole subtree to a commit.
    Atomic — one commit covering every restored AND removed file."""
    project_dir = project_dir.resolve()
    project_name = project_dir.name
    rel = backend.relpath(project_dir)
    checks = _common_checks(backend, project_dir, project_name)

    target_sha = ""
    files_at_target: list[str] = []
    if backend.commit_exists(target_commit):
        target_sha = backend.rev_parse(target_commit)
        files_at_target = backend.ls_files_at(target_sha, rel)
        detail = f"commit {target_sha[:8]} exists in history"
        ok = True
        if not files_at_target:
            ok = False
            detail = (
                f"commit {target_sha[:8]} exists but contains no files under "
                f"{rel} — refusing (would delete the whole project)"
            )
        checks.append(
            PreflightCheck(name="target_exists", ok=ok, detail=detail)
        )
    else:
        checks.append(
            PreflightCheck(
                name="target_exists",
                ok=False,
                detail=f"target ref {target_commit!r} not found in history",
            )
        )

    files_now = backend.ls_files_at("HEAD", rel) if target_sha else []
    removed = sorted(set(files_now) - set(files_at_target))
    changed = backend.run_git(
        "diff", "--name-only", f"{target_sha or 'HEAD'}..HEAD", "--", rel
    ).splitlines()
    affected = sorted({*changed, *removed})
    return RollbackPlan(
        granularity="project",
        project_name=project_name,
        project_dir=project_dir,
        artifact=f"project {project_name!r} subtree",
        current="HEAD",
        target=target_sha or target_commit,
        commit_message=(
            f"rollback({project_name}): bulk to {target_sha[:8] or target_commit} "
            f"({len(affected)} files; {len(removed)} removed)"
        ),
        changes=[f"restore {rel}/ to {target_sha[:8] or target_commit}"]
        + [f"  {f}" for f in affected],
        files=[rel],
        removed_files=removed,
        checks=checks,
    )


# --- execution ---------------------------------------------------------------------------


def enforce_preflight(
    plan: RollbackPlan, *, force: bool = False, assume_yes: bool = False
) -> list[str]:
    """Raise on any non-bypassed failing check; return the bypasses used."""
    overrides: list[str] = []
    for check in plan.checks:
        if check.ok:
            continue
        if check.bypass == "force" and force:
            overrides.append(check.name)
            continue
        if check.bypass == "confirm" and (assume_yes or force):
            overrides.append(check.name)
            continue
        hint = {
            "force": " (bypass with --force; the override is logged)",
            "confirm": " (confirm with --yes or --force; logged)",
            "none": "",
        }[check.bypass]
        raise RollbackError(
            f"pre-flight check {check.name!r} failed: {check.detail}{hint}",
            context={
                "check": check.name,
                "detail": check.detail,
                "project": plan.project_name,
                "granularity": plan.granularity,
            },
        )
    return overrides


def execute_rollback(
    plan: RollbackPlan,
    *,
    backend: GitBackend,
    operator: Operator | None = None,
    force: bool = False,
    assume_yes: bool = False,
) -> RollbackResult:
    """Apply a planned rollback: pin edit / subtree restore + ONE commit +
    audit entry. Pre-flight failures (not bypassed) abort before any write."""
    overrides = enforce_preflight(plan, force=force, assume_yes=assume_yes)
    operator = operator or resolve_operator(git_email=backend.user_email())
    op_run_id = str(RunId.new())
    logger = run_logger(op_run_id)

    with foundry_span(
        "foundry.rollback",
        {
            "run_id": op_run_id,
            "project": plan.project_name,
            "granularity": plan.granularity,
            "target_ref": plan.target,
            "files_affected_count": len(plan.files) + len(plan.removed_files),
            "operator": operator.kind,
            "overrides_used": ",".join(overrides),
        },
    ):
        if plan.granularity in ("tool", "prompt"):
            assert plan.txn is not None
            plan.txn.apply()
            commit_sha = backend.commit(
                [str(f) for f in plan.files], plan.commit_message
            )
        else:
            commit_sha = _apply_project_rollback(plan, backend)

        notes = _cache_notes(plan)
        entry = new_audit_entry(
            type="rollback",
            scope=f"{plan.project_name}/{plan.artifact}",
            summary=f"{plan.artifact}: {plan.current} → {plan.target}",
            operator=operator,
            commit_sha=commit_sha,
            files_affected=sorted({*plan.files, *plan.removed_files}),
            overrides_used=overrides,
            rationale="; ".join(notes) or None,
        )
        # ONE id for span + logs + audit so the trail joins up.
        entry = entry.model_copy(update={"id": op_run_id})
        append_audit_entry(plan.project_dir, entry)
        logger.info(
            "rollback.applied",
            project=plan.project_name,
            granularity=plan.granularity,
            target=plan.target,
            commit_sha=commit_sha,
            overrides_used=overrides,
        )
    return RollbackResult(
        commit_sha=commit_sha,
        audit_entry=entry,
        overrides_used=overrides,
        notes=notes,
    )


def _cache_notes(plan: RollbackPlan) -> list[str]:
    """Cache-effect acknowledgements (docs/52 § Rollback semantics for
    caches). v1 cache backends are keyed on agent_version / tool version, so
    rolled-back entries become unreachable by construction — the note makes
    the effect visible to operators."""
    if plan.granularity == "prompt":
        return [
            f"semantic cache entries for agent {plan.artifact} are now "
            "unreachable (agent_version changed)"
        ]
    if plan.granularity == "tool":
        return [
            f"tool-result cache entries for {plan.artifact}@{plan.current} are "
            f"now unreachable; {plan.target} entries (if any) become live again"
        ]
    return ["all agent_versions in the project may have changed; caches re-key"]


def _apply_project_rollback(plan: RollbackPlan, backend: GitBackend) -> str:
    """checkout <target> -- <project>/ + explicit removal of files added
    since + ONE commit. On any failure the subtree is restored to HEAD —
    all files or none (docs/03 exit gate).

    Staging and recovery operate on the COMPUTED file set (files tracked at
    the target + files being removed) — never ``add -A`` / ``clean -fd`` on
    the whole subtree, which on a ``--force``d dirty tree would sweep
    uncommitted operator files into the rollback commit (or delete untracked
    ones during recovery). Phase 5 review finding 1.
    """
    rel = plan.files[0]
    files_at_target = backend.ls_files_at(plan.target, rel)
    files_at_head = backend.ls_files_at("HEAD", rel)
    staged = sorted({*files_at_target, *plan.removed_files})
    try:
        backend.checkout_paths(plan.target, [rel])
        for removed in plan.removed_files:
            path = backend.repo_root / removed
            if path.exists():
                path.unlink()
        backend.run_git("add", "--", *staged)
        if not backend.run_git("diff", "--cached", "--name-only", "--", rel).strip():
            raise RollbackError(
                f"project subtree {rel} is already identical to "
                f"{plan.target[:8]}; nothing to roll back",
                context={"project": plan.project_name, "target": plan.target},
            )
        backend.run_git("commit", "-m", plan.commit_message)
        return backend.rev_parse("HEAD")
    except Exception:
        # Restore ONLY the computed set to HEAD: files tracked at HEAD go
        # back (index + worktree); files that exist at target but not at
        # HEAD (restored by checkout_paths above) are explicitly deleted.
        # Untracked operator files elsewhere in the subtree are untouched.
        if files_at_head:
            backend.run_git(
                "restore", "--source=HEAD", "--staged", "--worktree", "--",
                *files_at_head, check=False,
            )
        for extra in sorted(set(files_at_target) - set(files_at_head)):
            path = backend.repo_root / extra
            if path.exists():
                path.unlink()
            backend.run_git("rm", "--cached", "--ignore-unmatch", "-q", "--",
                            extra, check=False)
        raise


__all__ = [
    "Granularity",
    "PreflightCheck",
    "RollbackPlan",
    "RollbackResult",
    "enforce_preflight",
    "execute_rollback",
    "plan_project_rollback",
    "plan_prompt_rollback",
    "plan_tool_rollback",
]
