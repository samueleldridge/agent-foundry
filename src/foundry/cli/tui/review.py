"""`foundry review <project>` — the Phase 9 review TUI (docs/52 § Review
TUI; docs/82 § `foundry review`).

Dependency decision (Phase 9, locked): NO ``textual``. The TUI is built on
``rich`` — already present as typer's dependency — as a simple interactive
page: render one full screen with rich, read a command via ``input()``,
loop. That keeps the surface a plain readline-friendly REPL (works over ssh
and in CI transcripts) and keeps the dependency set unchanged. Tests drive
the PROGRAMMATIC layer (:class:`ReviewModel`), not the interactive loop.

Layers:

- :class:`ReviewModel` — pure data: commits + audit kinds + eval deltas,
  commit detail (project-scoped diff), per-artifact pins, eval trajectory,
  pending approvals, connections, and the (only) write action: per-project
  rollback via the Phase 5 planners.
- :func:`screen_text` — renders one screen (tabs: commits / evals /
  approvals / connections) to a string via a recording rich Console.
- :func:`run_review_loop` / :func:`execute_review` — the interactive shell.

Read-only by default except the rollback action (docs/52): no edits, no
commits — those go through the standard CLI commands.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from typing import Any

import yaml
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from foundry.cli._helpers import print_foundry_error, resolve_project_dir
from foundry.core.errors import FoundryError, GitBackendError, RollbackError
from foundry.eval import list_eval_history
from foundry.storage.paths import runs_root
from foundry.versioning.audit import AuditEntry, read_audit_entries
from foundry.versioning.git_backend import GitBackend
from foundry.versioning.rollback import execute_rollback, plan_project_rollback

TABS = ("commits", "evals", "approvals", "connections")

_FIELD_SEP = "\x1f"
_DIFF_PREVIEW_LINES = 40
_FOOTER = "[r] rollback  [d] full diff  [t] tab  [q] quit"


# --- row models ------------------------------------------------------------------


@dataclass(frozen=True)
class CommitRow:
    short_sha: str
    sha: str
    kind: str
    """Audit type of the matching audit entry ('forge', 'rollback', ...);
    'human' when no audit entry recorded the commit."""
    subject: str
    date: str
    eval_delta: float | None
    """after_score - before_score from the audit entry's eval context."""


@dataclass(frozen=True)
class CommitDetail:
    sha: str
    short_sha: str
    subject: str
    author: str
    date: str
    diff: str
    """Diff against the parent, scoped to the project subtree (root commit:
    the commit's own diff against the empty tree)."""
    summary: str | None
    operator: str | None
    eval_before: float | None
    eval_after: float | None


@dataclass(frozen=True)
class ArtifactRow:
    kind: str
    """'project' | 'prompt' | 'tool' | 'connection'."""
    name: str
    ref: str
    pinned: str
    eval_score: float | None
    """Latest eval_history score whose target_ref matches this artifact."""
    eval_at: str | None


@dataclass(frozen=True)
class EvalRow:
    eval_run_id: str
    eval_name: str
    target_version: str
    score: float | None
    passed: bool
    completed_at: str


@dataclass(frozen=True)
class ApprovalRow:
    run_id: str
    approval_id: str
    agent: str
    prompt: str


@dataclass(frozen=True)
class ConnectionRow:
    name: str
    ref: str
    version: str


# --- programmatic layer ----------------------------------------------------------


class ReviewModel:
    """Everything the review screens show, as plain rows. No rendering,
    no I/O beyond git/audit/eval-history/run-artifact reads (and the one
    write action, :meth:`rollback_to`)."""

    def __init__(self, project_dir: Path) -> None:
        self.project_dir = Path(project_dir).resolve()
        self.project_name = self.project_dir.name
        self._backend: GitBackend | None = None

    @property
    def backend(self) -> GitBackend:
        if self._backend is None:
            self._backend = GitBackend.discover(self.project_dir)
        return self._backend

    # -- commits ------------------------------------------------------------------

    def _audit_by_sha(self) -> dict[str, AuditEntry]:
        """Latest audit entry per commit sha (entries are oldest-first)."""
        by_sha: dict[str, AuditEntry] = {}
        for entry in read_audit_entries(self.project_dir):
            if entry.commit_sha:
                by_sha[entry.commit_sha] = entry
        return by_sha

    def commits(self, limit: int = 20) -> list[CommitRow]:
        """Recent commits touching the project subtree, newest first, with
        the audit kind + eval delta attached where an audit entry exists."""
        rel = self.backend.relpath(self.project_dir)
        audit = self._audit_by_sha()
        rows: list[CommitRow] = []
        for commit in self.backend.log(limit, paths=[rel]):
            entry = audit.get(commit.sha)
            delta: float | None = None
            if (
                entry is not None
                and entry.eval is not None
                and entry.eval.before_score is not None
                and entry.eval.after_score is not None
            ):
                delta = entry.eval.after_score - entry.eval.before_score
            rows.append(
                CommitRow(
                    short_sha=commit.short_sha,
                    sha=commit.sha,
                    kind=entry.type if entry is not None else "human",
                    subject=commit.subject,
                    date=commit.date,
                    eval_delta=delta,
                )
            )
        return rows

    def commit_detail(self, sha: str) -> CommitDetail:
        """One commit: project-scoped diff + audit context when known."""
        full = self.backend.rev_parse(sha)
        rel = self.backend.relpath(self.project_dir)
        try:
            diff = self.backend.diff(f"{full}^", full, paths=[rel])
        except GitBackendError:
            # Root commit — no parent; show the commit's own diff.
            diff = self.backend.run_git(
                "show", "--format=", "--end-of-options", full, "--", rel
            )
        line = self.backend.run_git(
            "log",
            "-n1",
            f"--pretty=format:%an{_FIELD_SEP}%aI{_FIELD_SEP}%s",
            "--end-of-options",
            full,
        ).strip()
        author, date, subject = line.split(_FIELD_SEP, 2)

        entry = self._audit_by_sha().get(full)
        summary: str | None = None
        operator: str | None = None
        eval_before: float | None = None
        eval_after: float | None = None
        if entry is not None:
            summary = entry.summary
            operator = _render_operator(entry)
            if entry.eval is not None:
                eval_before = entry.eval.before_score
                eval_after = entry.eval.after_score
        return CommitDetail(
            sha=full,
            short_sha=full[:8],
            subject=subject,
            author=author,
            date=date,
            diff=diff,
            summary=summary,
            operator=operator,
            eval_before=eval_before,
            eval_after=eval_after,
        )

    # -- artifacts / evals ----------------------------------------------------------

    def _raw_system(self) -> dict[str, Any]:
        data = yaml.safe_load((self.project_dir / "system.yaml").read_text())
        return data if isinstance(data, dict) else {}

    def artifact_versions(self) -> list[ArtifactRow]:
        """Per-artifact pin rows (project + agent prompts + tool/connection
        pins from system.yaml) with the latest matching eval score."""
        system = self._raw_system()
        history = list_eval_history(self.project_dir)

        rows: list[ArtifactRow] = []
        project_eval = _latest_eval(
            history, matches={self.project_name}, scope="project"
        )
        rows.append(
            _artifact_row(
                kind="project",
                name=self.project_name,
                ref=self.project_name,
                pinned=self._head_short_sha(),
                hit=project_eval,
            )
        )

        agents = system.get("agents") or []
        if isinstance(agents, list):
            for agent in agents:
                name = str(agent)
                rows.append(
                    _artifact_row(
                        kind="prompt",
                        name=name,
                        ref=f"agents/{name}",
                        pinned=self._prompt_pin(name),
                        hit=_latest_eval(history, matches={name}),
                    )
                )

        for kind, key in (("tool", "tools"), ("connection", "connections")):
            block = system.get(key) or {}
            if not isinstance(block, dict):
                continue
            for logical, binding in sorted(block.items()):
                if not isinstance(binding, dict):
                    continue
                ref = str(binding.get("ref", ""))
                pinned = str(binding.get("version", ""))
                hit = _latest_eval(
                    history,
                    matches={str(logical), ref, f"{ref}@{pinned}"},
                )
                rows.append(
                    _artifact_row(
                        kind=kind,
                        name=str(logical),
                        ref=ref,
                        pinned=pinned,
                        hit=hit,
                    )
                )
        return rows

    def eval_trajectory(self) -> list[EvalRow]:
        """Project-scope eval_history rows, oldest first (docs/40)."""
        rows: list[EvalRow] = []
        for raw in list_eval_history(self.project_dir):
            if raw.get("scope") != "project":
                continue
            score = raw.get("score")
            rows.append(
                EvalRow(
                    eval_run_id=str(raw.get("eval_run_id", "")),
                    eval_name=str(raw.get("eval_name", "")),
                    target_version=str(raw.get("target_version", "")),
                    score=float(score) if isinstance(score, int | float) else None,
                    passed=bool(raw.get("passed", False)),
                    completed_at=str(raw.get("completed_at", "")),
                )
            )
        return rows

    # -- approvals / connections -----------------------------------------------------

    def pending_approvals(self) -> list[ApprovalRow]:
        """approval_pending runs for this project from the FOUNDRY_HOME run
        artifacts (same metadata.json scan as `foundry approvals list`)."""
        root = runs_root()
        rows: list[ApprovalRow] = []
        if not root.is_dir():
            return rows
        for directory in sorted(root.iterdir()):
            metadata = _read_run_metadata(directory / "metadata.json")
            if metadata is None or metadata.get("status") != "approval_pending":
                continue
            if metadata.get("project") != self.project_name:
                continue
            pending = metadata.get("pending_approval") or {}
            if not isinstance(pending, dict):
                pending = {}
            rows.append(
                ApprovalRow(
                    run_id=directory.name,
                    approval_id=str(pending.get("approval_id", "?")),
                    agent=str(pending.get("agent_name", "?")),
                    prompt=str(pending.get("prompt", ""))[:60],
                )
            )
        return rows

    def connections(self) -> list[ConnectionRow]:
        """Bound connections from the raw system.yaml `connections:` block."""
        block = self._raw_system().get("connections") or {}
        rows: list[ConnectionRow] = []
        if not isinstance(block, dict):
            return rows
        for logical, binding in sorted(block.items()):
            if not isinstance(binding, dict):
                continue
            rows.append(
                ConnectionRow(
                    name=str(logical),
                    ref=str(binding.get("ref", "")),
                    version=str(binding.get("version", "")),
                )
            )
        return rows

    # -- the one write action -----------------------------------------------------------

    def rollback_to(self, sha: str, *, assume_yes: bool) -> str:
        """Per-project rollback to ``sha`` via the Phase 5 planner +
        executor (pre-flight checks, ONE commit, audit entry). Returns the
        new commit sha."""
        plan = plan_project_rollback(self.project_dir, sha, backend=self.backend)
        result = execute_rollback(
            plan, backend=self.backend, assume_yes=assume_yes
        )
        return result.commit_sha

    # -- internals ---------------------------------------------------------------------

    def _head_short_sha(self) -> str:
        try:
            return self.backend.rev_parse("HEAD")[:8]
        except GitBackendError:
            return ""

    def _prompt_pin(self, agent: str) -> str:
        agent_yaml = self.project_dir / "agents" / agent / "agent.yaml"
        if not agent_yaml.is_file():
            return ""
        data = yaml.safe_load(agent_yaml.read_text())
        prompt = data.get("prompt") if isinstance(data, dict) else None
        return str(prompt.get("version", "")) if isinstance(prompt, dict) else ""


def _render_operator(entry: AuditEntry) -> str:
    op = entry.operator
    if op.kind == "meta_agent":
        rendered = f"meta_agent (forge {op.forge_run_id or '?'})"
        if op.human_supervisor:
            rendered += f", supervisor {op.human_supervisor}"
        return rendered
    return f"{op.kind} ({op.human_email or 'unknown'})"


def _latest_eval(
    history: list[dict[str, Any]],
    *,
    matches: set[str],
    scope: str | None = None,
) -> dict[str, Any] | None:
    """The newest eval_history row whose target_ref is in ``matches``
    (rows are oldest-first, so the last hit wins)."""
    hit: dict[str, Any] | None = None
    for raw in history:
        if scope is not None and raw.get("scope") != scope:
            continue
        if str(raw.get("target_ref", "")) in matches:
            hit = raw
    return hit


def _artifact_row(
    *, kind: str, name: str, ref: str, pinned: str, hit: dict[str, Any] | None
) -> ArtifactRow:
    score: float | None = None
    at: str | None = None
    if hit is not None:
        raw_score = hit.get("score")
        score = float(raw_score) if isinstance(raw_score, int | float) else None
        at = str(hit.get("completed_at", "")) or None
    return ArtifactRow(
        kind=kind, name=name, ref=ref, pinned=pinned, eval_score=score, eval_at=at
    )


def _read_run_metadata(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


# --- rendering -------------------------------------------------------------------------


def screen_text(
    model: ReviewModel, *, tab: str = "commits", selected: int = 0, limit: int = 20
) -> str:
    """One full review screen as plain text (recording rich Console)."""
    console = Console(
        file=StringIO(),
        record=True,
        width=100,
        markup=False,
        highlight=False,
        emoji=False,
    )
    tab = tab if tab in TABS else "commits"
    console.print(f"foundry review {model.project_name}")
    console.print(
        "tabs: " + " / ".join(f"<{t}>" if t == tab else t for t in TABS)
    )
    console.print()
    if tab == "commits":
        _render_commits(console, model, selected=selected, limit=limit)
    elif tab == "evals":
        _render_evals(console, model)
    elif tab == "approvals":
        _render_approvals(console, model)
    else:
        _render_connections(console, model)
    console.print(_FOOTER)
    return console.export_text()


def _render_commits(
    console: Console, model: ReviewModel, *, selected: int, limit: int
) -> None:
    rows = model.commits(limit)
    table = Table(box=box.SIMPLE, pad_edge=False)
    for column in ("", "sha", "kind", "subject", "eval Δ", "date"):
        table.add_column(column, overflow="fold")
    for index, row in enumerate(rows):
        delta = f"{row.eval_delta:+.2f}" if row.eval_delta is not None else ""
        table.add_row(
            ">" if index == _clamp(selected, len(rows)) else "",
            row.short_sha,
            row.kind,
            row.subject,
            delta,
            row.date[:19],
        )
    console.print(table)
    if not rows:
        console.print("(no commits touch this project yet)")
        return
    detail = model.commit_detail(rows[_clamp(selected, len(rows))].sha)
    body_lines = detail.diff.splitlines()
    truncated = len(body_lines) > _DIFF_PREVIEW_LINES
    body_lines = body_lines[:_DIFF_PREVIEW_LINES]
    if truncated:
        body_lines.append("… (press d for the full diff)")
    if not body_lines:
        body_lines = ["(empty diff under the project subtree)"]
    if detail.eval_before is not None or detail.eval_after is not None:
        body_lines += [
            "",
            "Eval context:",
            f"  before: {_fmt_score(detail.eval_before)}",
            f"  after:  {_fmt_score(detail.eval_after)}",
        ]
    if detail.summary:
        body_lines += ["", f"Audit: {detail.summary}"]
    if detail.operator:
        body_lines.append(f"Operator: {detail.operator}")
    console.print(
        Panel(
            "\n".join(body_lines),
            title=f"Selected: {detail.short_sha}  {detail.subject}",
            title_align="left",
        )
    )


def _render_evals(console: Console, model: ReviewModel) -> None:
    rows = model.eval_trajectory()
    if not rows:
        console.print("(no project-scope eval history)")
        return
    table = Table(box=box.SIMPLE, pad_edge=False)
    for column in ("eval_run_id", "eval", "target", "score", "passed", "completed"):
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(
            row.eval_run_id[:12],
            row.eval_name,
            row.target_version,
            _fmt_score(row.score),
            "yes" if row.passed else "no",
            row.completed_at[:19],
        )
    console.print(table)


def _render_approvals(console: Console, model: ReviewModel) -> None:
    rows = model.pending_approvals()
    if not rows:
        console.print("(no pending approvals)")
        return
    table = Table(box=box.SIMPLE, pad_edge=False)
    for column in ("run_id", "approval_id", "agent", "prompt"):
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(row.run_id, row.approval_id, row.agent, row.prompt)
    console.print(table)


def _render_connections(console: Console, model: ReviewModel) -> None:
    rows = model.connections()
    if not rows:
        console.print("(no connections bound in system.yaml)")
        return
    table = Table(box=box.SIMPLE, pad_edge=False)
    for column in ("connection", "ref", "version"):
        table.add_column(column, overflow="fold")
    for row in rows:
        table.add_row(row.name, row.ref, row.version)
    console.print(table)


def _fmt_score(score: float | None) -> str:
    return f"{score:.2f}" if score is not None else "?"


def _clamp(selected: int, count: int) -> int:
    return max(0, min(selected, count - 1))


# --- interactive loop ---------------------------------------------------------------------


def run_review_loop(project: str) -> int:
    """The input()-driven review shell. Commands: j/k (or up/down), show N,
    tab <name>, t (cycle), d (full diff), r (rollback, confirm by typing the
    short sha), q (quit). EOF exits cleanly."""
    project_dir = resolve_project_dir(project)
    model = ReviewModel(project_dir)
    tab = "commits"
    selected = 0
    limit = 20
    while True:
        print(screen_text(model, tab=tab, selected=selected, limit=limit))
        try:
            raw = input("review> ")
        except EOFError:
            return 0
        command = raw.strip()
        lowered = command.lower()
        if lowered in ("q", "quit", "exit"):
            return 0
        if lowered in ("j", "down"):
            selected += 1
        elif lowered in ("k", "up"):
            selected = max(0, selected - 1)
        elif lowered.startswith("show "):
            tail = lowered.removeprefix("show ").strip()
            if tail.isdigit() and int(tail) > 0:
                limit = int(tail)
            else:
                print("usage: show <N>  (positive commit count)")
        elif lowered.startswith("tab "):
            wanted = lowered.removeprefix("tab ").strip()
            if wanted in TABS:
                tab = wanted
            else:
                print(f"unknown tab {wanted!r}; one of: {', '.join(TABS)}")
        elif lowered == "t":
            tab = TABS[(TABS.index(tab) + 1) % len(TABS)]
        elif lowered == "d":
            row = _selected_commit(model, selected, limit)
            if row is None:
                print("(no commit selected)")
            else:
                print(model.commit_detail(row.sha).diff or "(empty diff)")
        elif lowered == "r":
            code = _rollback_command(model, tab, selected, limit)
            if code is not None:
                return code
            selected = 0
        elif lowered:
            print(
                "commands: j/k (or up/down) move · show N · tab "
                f"<{'/'.join(TABS)}> · t cycle · d full diff · r rollback · q quit"
            )


def _selected_commit(
    model: ReviewModel, selected: int, limit: int
) -> CommitRow | None:
    rows = model.commits(limit)
    if not rows:
        return None
    return rows[_clamp(selected, len(rows))]


def _rollback_command(
    model: ReviewModel, tab: str, selected: int, limit: int
) -> int | None:
    """The `r` command. Returns an exit code to terminate the loop (EOF
    during confirmation) or None to continue."""
    if tab != "commits":
        print("rollback: switch to the commits tab first (tab commits)")
        return None
    row = _selected_commit(model, selected, limit)
    if row is None:
        print("(no commit selected)")
        return None
    try:
        confirm = input(
            f"type {row.short_sha} to confirm PROJECT rollback to "
            f"{row.subject!r} (anything else aborts): "
        )
    except EOFError:
        return 0
    if confirm.strip() != row.short_sha:
        print("aborted; nothing changed")
        return None
    try:
        new_sha = model.rollback_to(row.sha, assume_yes=True)
    except RollbackError as exc:
        print_foundry_error(exc)
        return None
    print(f"rolled back to {row.short_sha}; new commit {new_sha[:8]}")
    return None


def execute_review(project: str) -> int:
    """The `foundry review` executor. 0 clean exit, 2 configuration error."""
    try:
        return run_review_loop(project)
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2


__all__ = [
    "TABS",
    "ApprovalRow",
    "ArtifactRow",
    "CommitDetail",
    "CommitRow",
    "ConnectionRow",
    "EvalRow",
    "ReviewModel",
    "execute_review",
    "run_review_loop",
    "screen_text",
]
