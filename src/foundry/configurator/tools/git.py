"""Versioning meta-tools: ``git_commit`` / ``git_show`` / ``list_versions``
(docs/61 § Versioning; docs/51 § The meta-agent's git operations).

The sandbox here is the trust boundary that makes letting an LLM commit to
a real repository acceptable:

- **Forbidden operations** (push, pull, fetch, rebase, reset, merge,
  checkout, tag, config, reflog, clean, any ``--force``) are rejected by
  :func:`ensure_allowed_git` BEFORE any subprocess runs. The meta-tools
  route every git invocation through it — belt and braces on top of the
  fact that no meta-tool even exposes those verbs.
- **Path scoping**: ``git_commit`` stages only files inside the scoped
  project.
- **Branch scoping**: every write op verifies the current branch is
  ``foundry/<scoped_project>`` first.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from foundry.configurator.tools.context import (
    MetaToolContext,
    RecordedCommit,
    resolve_path,
    violation,
)
from foundry.core.errors import ConfigError, GitBackendError
from foundry.core.tool import RunContext
from foundry.versioning.artifacts import (
    list_artifact_versions,
    list_prompt_versions,
    prompts_dir,
)
from foundry.versioning.audit import (
    append_audit_entry,
    new_audit_entry,
    resolve_operator,
)

FORBIDDEN_GIT_VERBS = frozenset(
    {
        "push",
        "pull",
        "fetch",
        "rebase",
        "reset",
        "reflog",
        "checkout",
        "switch",
        "merge",
        "tag",
        "config",
        "clean",
        "branch",
        "remote",
        "gc",
        "submodule",
        "filter-branch",
    }
)

_FORBIDDEN_FLAGS = ("--force", "-f", "--force-with-lease", "--hard")


def ensure_allowed_git(*args: str) -> None:
    """Reject forbidden git operations BEFORE any subprocess is spawned
    (docs/51 § Forbidden operations). Raises ``GitBackendError``."""
    if not args:
        raise GitBackendError(
            "empty git invocation", context={"argv": list(args)}
        )
    verb = args[0]
    if verb in FORBIDDEN_GIT_VERBS:
        raise GitBackendError(
            f"git {verb} is forbidden for the meta-agent (docs/51 § "
            "Forbidden operations); humans drive that operation",
            context={"argv": list(args), "forbidden_verb": verb},
        )
    flagged = sorted(set(args[1:]) & set(_FORBIDDEN_FLAGS))
    if flagged:
        raise GitBackendError(
            f"forbidden git flag(s) {', '.join(flagged)} — force-class "
            "operations are never available to the meta-agent",
            context={"argv": list(args), "forbidden_flags": flagged},
        )


class GitCommitIn(BaseModel):
    """Structured commit message (docs/61 § git_commit): the tool formats
    the conventional ``forge(...)`` text; the meta-agent supplies parts."""

    model_config = ConfigDict(extra="forbid")

    files: list[str] = Field(min_length=1)
    scope: str
    """'<project>' or '<project>/<artifact path>', e.g.
    'pipeline_recon/agents/investigator'."""
    summary: str = Field(min_length=3, max_length=120)
    body: str = ""
    cluster_id: str | None = None
    eval_before: float | None = None
    eval_after: float | None = None


class CommitResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit_sha: str
    message: str
    files_committed: list[str]
    audit_entry_id: str


def _require_branch(mctx: MetaToolContext, *, tool: str) -> None:
    current = mctx.backend.current_branch()
    if current != mctx.branch:
        raise GitBackendError(
            f"{tool}: expected branch {mctx.branch!r}, found {current!r}; "
            "checkout the project branch before invoking forge (docs/51 "
            "§ Branch sandbox)",
            context={"expected": mctx.branch, "current": current},
        )


def format_forge_message(mctx: MetaToolContext, inputs: GitCommitIn) -> str:
    before = "?" if inputs.eval_before is None else f"{inputs.eval_before:.2f}"
    after = "?" if inputs.eval_after is None else f"{inputs.eval_after:.2f}"
    lines = [f"forge({inputs.scope}): {inputs.summary}"]
    if inputs.body:
        lines += ["", inputs.body]
    lines += [
        "",
        f"Iteration: {mctx.forge_run_id} | Eval: {before} -> {after} | "
        f"Cluster: {inputs.cluster_id or '-'}",
    ]
    return "\n".join(lines)


def make_git_commit(
    mctx: MetaToolContext,
) -> Callable[[GitCommitIn, RunContext], Awaitable[CommitResult]]:
    async def handle(inputs: GitCommitIn, ctx: RunContext) -> CommitResult:
        if not (
            inputs.scope == mctx.scoped_project
            or inputs.scope.startswith(f"{mctx.scoped_project}/")
        ):
            raise ConfigError(
                f"git_commit: scope {inputs.scope!r} must reference the "
                f"scoped project ({mctx.scoped_project!r} or "
                f"'{mctx.scoped_project}/<artifact>')",
                context={"scope": inputs.scope},
            )
        _require_branch(mctx, tool="git_commit")
        rel_files: list[str] = []
        for raw in inputs.files:
            path = resolve_path(mctx, raw)
            if not path.is_relative_to(mctx.project_dir):
                violation(
                    mctx,
                    ctx.session,
                    tool="git_commit",
                    detail=f"file outside the scoped project: {path}",
                )
            rel_files.append(mctx.backend.relpath(path))
        message = format_forge_message(mctx, inputs)
        ensure_allowed_git("add", "--", *rel_files)
        ensure_allowed_git("commit", "-m", message)
        sha = mctx.backend.commit(list(rel_files), message)
        operator = resolve_operator(
            git_email=mctx.git_email, forge_run_id=mctx.forge_run_id
        )
        entry = new_audit_entry(
            type="forge",
            scope=inputs.scope,
            summary=inputs.summary,
            operator=operator,
            commit_sha=sha,
            files_affected=rel_files,
            rationale=inputs.body or None,
        )
        append_audit_entry(mctx.project_dir, entry)
        mctx.records.commits.append(
            RecordedCommit(sha=sha, message=message, files=rel_files)
        )
        return CommitResult(
            commit_sha=sha,
            message=message,
            files_committed=rel_files,
            audit_entry_id=entry.id,
        )

    return handle


class GitShowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit_sha: str


class CommitDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    commit_sha: str
    message: str
    files_changed: list[str]
    diff: str


def make_git_show(
    mctx: MetaToolContext,
) -> Callable[[GitShowIn, RunContext], Awaitable[CommitDetail]]:
    async def handle(inputs: GitShowIn, ctx: RunContext) -> CommitDetail:
        ensure_allowed_git("show", inputs.commit_sha)
        sha = mctx.backend.rev_parse(inputs.commit_sha)
        # Reachability: the commit must be on the scoped project's branch.
        containing = mctx.backend.run_git(
            "branch", "--contains", sha, "--format=%(refname:short)",
        )
        expected = (
            mctx.branch
            if mctx.backend.branch_exists(mctx.branch)
            else mctx.backend.current_branch()
        )
        if expected not in containing.split():
            raise GitBackendError(
                f"git_show: commit {sha[:8]} is not reachable from "
                f"{expected!r}; the meta-agent inspects its own branch only",
                context={"commit": sha, "branch": expected},
            )
        rel = mctx.backend.relpath(mctx.project_dir)
        message = mctx.backend.run_git(
            "log", "-1", "--pretty=%B", "--end-of-options", sha
        ).strip()
        files = [
            line
            for line in mctx.backend.run_git(
                "show", "--name-only", "--pretty=format:", "--end-of-options",
                sha,
            ).splitlines()
            if line.strip()
        ]
        diff = mctx.backend.run_git(
            "show", "--end-of-options", sha, "--", rel
        )
        return CommitDetail(
            commit_sha=sha, message=message, files_changed=files, diff=diff
        )

    return handle


class ListVersionsIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str | None = None
    """None → recent commits on the project branch; 'tool/<name>' →
    directory versions; 'agent/<name>/prompts' → prompt files;
    'connection/<name>' → directory versions."""


class VersionEntryOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    identifier: str
    summary: str = ""
    timestamp: str = ""


class VersionListing(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    kind: Literal["commits", "directory_versions", "file_versions"]
    entries: list[VersionEntryOut]


def make_list_versions(
    mctx: MetaToolContext,
) -> Callable[[ListVersionsIn, RunContext], Awaitable[VersionListing]]:
    async def handle(inputs: ListVersionsIn, ctx: RunContext) -> VersionListing:
        if inputs.target is None:
            rel = mctx.backend.relpath(mctx.project_dir)
            commits = mctx.backend.log(20, paths=[rel])
            return VersionListing(
                target="commits",
                kind="commits",
                entries=[
                    VersionEntryOut(
                        identifier=c.sha,
                        summary=c.subject,
                        timestamp=c.date,
                    )
                    for c in commits
                ],
            )
        parts = inputs.target.split("/")
        if len(parts) == 3 and parts[0] == "agent" and parts[2] == "prompts":
            versions = list_prompt_versions(
                prompts_dir(mctx.project_dir, parts[1])
            )
            return VersionListing(
                target=inputs.target,
                kind="file_versions",
                entries=[VersionEntryOut(identifier=v) for v in versions],
            )
        if len(parts) == 2 and parts[0] in ("tool", "connection", "retriever"):
            directory = mctx.project_dir / f"{parts[0]}s" / parts[1]
            versions = (
                list_artifact_versions(directory) if directory.is_dir() else []
            )
            return VersionListing(
                target=inputs.target,
                kind="directory_versions",
                entries=[VersionEntryOut(identifier=v) for v in versions],
            )
        raise ConfigError(
            f"list_versions: unknown target {inputs.target!r}; expected "
            "None, 'tool/<name>', 'connection/<name>', or "
            "'agent/<name>/prompts'",
            context={"target": inputs.target},
        )

    return handle


__all__ = [
    "FORBIDDEN_GIT_VERBS",
    "CommitDetail",
    "CommitResult",
    "GitCommitIn",
    "GitShowIn",
    "ListVersionsIn",
    "VersionEntryOut",
    "VersionListing",
    "ensure_allowed_git",
    "format_forge_message",
    "make_git_commit",
    "make_git_show",
    "make_list_versions",
]
