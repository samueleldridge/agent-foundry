"""Thin subprocess wrapper around ``git`` (docs/51 § Subprocess wrapping).

Deliberately a subprocess wrapper, NOT a Python git library: same behaviour
as the operator's own ``git``, predictable stderr, no extra dependency. The
Phase 6 meta-agent's git tools wrap THIS module (and add the sandbox: path
scoping, branch checks, forbidden-op guards — per docs/51 those guards live
at the meta-tool layer, not here).

Every failure raises :class:`~foundry.core.errors.GitBackendError` with the
failing argv, returncode, and stderr in ``context``.

Sync deviation from the docs/51 sketch (which shows ``anyio.run_process``):
the Phase 5 consumers — CLI rollback/versions/diff/promote — are synchronous
commands, so the backend is sync ``subprocess.run``. Phase 6 can wrap calls
in ``anyio.to_thread`` without changing this surface.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

from foundry.core.errors import GitBackendError

_GIT_TIMEOUT_S = 60.0
_FIELD_SEP = "\x1f"


@dataclass(frozen=True)
class CommitInfo:
    """One `git log` line, parsed."""

    sha: str
    author: str
    date: str
    """Author date, strict ISO 8601 (``%aI``)."""
    subject: str

    @property
    def short_sha(self) -> str:
        return self.sha[:8]


class GitBackend:
    """All git operations the foundry performs, scoped to one repository."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root).resolve()

    @classmethod
    def discover(cls, path: Path) -> GitBackend:
        """Find the repository containing ``path`` (rev-parse --show-toplevel)."""
        probe = path if path.is_dir() else path.parent
        try:
            result = subprocess.run(
                ["git", "-C", str(probe), "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError as exc:  # pragma: no cover - env-dependent
            raise GitBackendError(
                "git binary not found in PATH",
                context={"path": str(path)},
                cause=exc,
            ) from exc
        if result.returncode != 0:
            raise GitBackendError(
                f"{path} is not inside a git repository: "
                f"{result.stderr.strip()}",
                context={"path": str(path), "stderr": result.stderr.strip()},
            )
        return cls(Path(result.stdout.strip()))

    # --- plumbing ---------------------------------------------------------

    def run_git(self, *args: str, check: bool = True) -> str:
        """Run ``git <args>`` at the repo root; return stdout."""
        argv = ["git", *args]
        try:
            result = subprocess.run(
                argv,
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                timeout=_GIT_TIMEOUT_S,
                check=False,
            )
        except FileNotFoundError as exc:  # pragma: no cover - env-dependent
            raise GitBackendError(
                "git binary not found in PATH",
                context={"argv": argv},
                cause=exc,
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise GitBackendError(
                f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_S}s",
                context={"argv": argv, "timeout_s": _GIT_TIMEOUT_S},
                cause=exc,
            ) from exc
        if check and result.returncode != 0:
            raise GitBackendError(
                f"git {' '.join(args)} failed: {result.stderr.strip()}",
                context={
                    "argv": argv,
                    "returncode": result.returncode,
                    "stdout": result.stdout.strip(),
                    "stderr": result.stderr.strip(),
                },
            )
        return result.stdout

    def relpath(self, path: Path) -> str:
        """``path`` relative to the repo root (POSIX separators)."""
        try:
            return Path(path).resolve().relative_to(self.repo_root).as_posix()
        except ValueError as exc:
            raise GitBackendError(
                f"{path} is outside the repository at {self.repo_root}",
                context={"path": str(path), "repo_root": str(self.repo_root)},
                cause=exc,
            ) from exc

    # --- read operations ----------------------------------------------------

    def rev_parse(self, ref: str = "HEAD") -> str:
        # ``--end-of-options`` on every path that interpolates a caller-
        # supplied ref: a ref like ``--upload-pack=...`` must reach git as a
        # revision (and fail), never as an option (Phase 5 review finding 4).
        return self.run_git(
            "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"
        ).strip()

    def commit_exists(self, ref: str) -> bool:
        try:
            self.rev_parse(ref)
        except GitBackendError:
            return False
        return True

    def current_branch(self) -> str:
        return self.run_git("rev-parse", "--abbrev-ref", "HEAD").strip()

    def branch_exists(self, name: str) -> bool:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{name}"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        return result.returncode == 0

    def log(
        self, limit: int = 10, *, paths: list[str] | None = None
    ) -> list[CommitInfo]:
        """Recent commits, newest first; ``paths`` are repo-relative filters.
        An unborn branch (no commits yet) yields an empty list."""
        args = [
            "log",
            f"-n{limit}",
            f"--pretty=format:%H{_FIELD_SEP}%an{_FIELD_SEP}%aI{_FIELD_SEP}%s",
        ]
        if paths:
            args += ["--", *paths]
        try:
            out = self.run_git(*args)
        except GitBackendError as exc:
            stderr = str(exc.context.get("stderr", ""))
            if "does not have any commits yet" in stderr:
                return []
            raise
        commits: list[CommitInfo] = []
        for line in out.splitlines():
            if not line.strip():
                continue
            sha, author, date, subject = line.split(_FIELD_SEP, 3)
            commits.append(
                CommitInfo(sha=sha, author=author, date=date, subject=subject)
            )
        return commits

    def show(self, ref: str) -> str:
        """The full ``git show`` output (message + diff) for one commit."""
        return self.run_git("show", "--end-of-options", ref)

    def diff(
        self, ref1: str, ref2: str, *, paths: list[str] | None = None
    ) -> str:
        args = ["diff", "--end-of-options", f"{ref1}..{ref2}"]
        if paths:
            args += ["--", *paths]
        return self.run_git(*args)

    def status_porcelain(self, *, paths: list[str] | None = None) -> str:
        args = ["status", "--porcelain"]
        if paths:
            args += ["--", *paths]
        return self.run_git(*args)

    def is_dirty(self, *, paths: list[str] | None = None) -> bool:
        """True when the working tree (or the given subtrees) has any
        uncommitted change — staged, unstaged, or untracked."""
        return bool(self.status_porcelain(paths=paths).strip())

    def ls_files_at(self, ref: str, path: str) -> list[str]:
        """Tracked files under ``path`` (repo-relative) at ``ref``."""
        out = self.run_git(
            "ls-tree", "-r", "--name-only", "--end-of-options", ref, "--", path
        )
        return [line for line in out.splitlines() if line.strip()]

    def user_email(self) -> str | None:
        """The operator's ``git config user.email`` — read-only; the foundry
        NEVER writes git config (docs/51 invariant)."""
        result = subprocess.run(
            ["git", "config", "user.email"],
            cwd=self.repo_root,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
        email = result.stdout.strip()
        return email if result.returncode == 0 and email else None

    # --- write operations -----------------------------------------------------

    def ensure_branch(self, name: str) -> str:
        """Make ``name`` the current branch, creating it at HEAD if missing.
        Returns the branch name."""
        if self.current_branch() == name:
            return name
        if self.branch_exists(name):
            self.run_git("checkout", name)
        else:
            self.run_git("checkout", "-b", name)
        return name

    def commit(self, files: list[Path | str], message: str) -> str:
        """Stage exactly ``files`` + commit atomically; returns the commit
        sha. One missing file aborts the whole operation with nothing
        committed (git verifies every pathspec before staging any)."""
        if not files:
            raise GitBackendError(
                "commit called with no files",
                context={"message": message},
            )
        rel = [f if isinstance(f, str) else self.relpath(f) for f in files]
        self.run_git("add", "--", *rel)
        self.run_git("commit", "-m", message)
        return self.rev_parse("HEAD")

    def revert(self, ref: str) -> str:
        """``git revert --no-edit <ref>``; returns the new commit sha."""
        self.run_git("revert", "--no-edit", "--end-of-options", ref)
        return self.rev_parse("HEAD")

    def checkout_paths(self, ref: str, paths: list[str]) -> None:
        """``git checkout <ref> -- <paths>`` — restore subtrees to ``ref``
        (both index and working tree). Files ADDED since ``ref`` are NOT
        removed by git; callers that need removal semantics (per-project
        rollback) delete them explicitly."""
        if not paths:
            raise GitBackendError(
                "checkout_paths called with no paths", context={"ref": ref}
            )
        self.run_git("checkout", "--end-of-options", ref, "--", *paths)


__all__ = ["CommitInfo", "GitBackend"]
