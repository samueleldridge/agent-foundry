"""GitBackend unit tests (docs/51 § Test expectations).

Every test runs against a THROWAWAY temp git repo built under tmp_path —
never against the real workspace (CLAUDE.md invariant for Phase 5 tests).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from foundry.core.errors import GitBackendError
from foundry.versioning.git_backend import GitBackend


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@example.com",
         "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        timeout=30,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "initial")
    return repo


@pytest.fixture
def backend(repo: Path) -> GitBackend:
    return GitBackend(repo)


@pytest.mark.unit
def test_run_git_success_returns_stdout(backend: GitBackend) -> None:
    assert backend.run_git("rev-parse", "--abbrev-ref", "HEAD").strip() == "main"


@pytest.mark.unit
def test_run_git_failure_raises_with_context(backend: GitBackend) -> None:
    with pytest.raises(GitBackendError) as exc_info:
        backend.run_git("rev-parse", "--verify", "no-such-ref")
    context = exc_info.value.context
    assert context["returncode"] != 0
    assert "argv" in context and "stderr" in context


@pytest.mark.unit
def test_discover_finds_repo_root_and_rejects_outside(
    repo: Path, tmp_path: Path
) -> None:
    nested = repo / "deep" / "dir"
    nested.mkdir(parents=True)
    assert GitBackend.discover(nested).repo_root == repo.resolve()
    outside = tmp_path / "not_a_repo"
    outside.mkdir()
    with pytest.raises(GitBackendError, match="not inside a git repository"):
        GitBackend.discover(outside)


@pytest.mark.unit
def test_commit_stages_exactly_the_listed_files(
    repo: Path, backend: GitBackend
) -> None:
    (repo / "b.txt").write_text("b\n")
    (repo / "unrelated.txt").write_text("stays unstaged\n")
    sha = backend.commit([repo / "b.txt"], "test(fixture): add b")
    assert backend.rev_parse("HEAD") == sha
    # unrelated file untouched — still untracked
    status = backend.status_porcelain()
    assert "?? unrelated.txt" in status
    assert "b.txt" not in status


@pytest.mark.unit
def test_commit_with_missing_file_aborts_atomically(
    repo: Path, backend: GitBackend
) -> None:
    """docs/51: one file missing -> git add fails, NOTHING staged, nothing
    committed."""
    (repo / "real.txt").write_text("real\n")
    head_before = backend.rev_parse("HEAD")
    with pytest.raises(GitBackendError):
        backend.commit([repo / "real.txt", repo / "missing.txt"], "boom")
    assert backend.rev_parse("HEAD") == head_before
    # nothing staged left over
    assert not backend.run_git("diff", "--cached", "--name-only").strip()


@pytest.mark.unit
def test_commit_empty_file_list_refused(backend: GitBackend) -> None:
    with pytest.raises(GitBackendError, match="no files"):
        backend.commit([], "empty")


@pytest.mark.unit
def test_ensure_branch_creates_switches_and_reuses(
    repo: Path, backend: GitBackend
) -> None:
    assert backend.ensure_branch("foundry/demo") == "foundry/demo"
    assert backend.current_branch() == "foundry/demo"
    backend.run_git("checkout", "-q", "main")
    assert backend.ensure_branch("foundry/demo") == "foundry/demo"  # reuse
    assert backend.current_branch() == "foundry/demo"
    assert backend.branch_exists("foundry/demo")
    assert not backend.branch_exists("foundry/other")


@pytest.mark.unit
def test_log_show_and_diff(repo: Path, backend: GitBackend) -> None:
    (repo / "a.txt").write_text("a2\n")
    backend.commit([repo / "a.txt"], "test(fixture): bump a")
    commits = backend.log(10)
    assert [c.subject for c in commits] == ["test(fixture): bump a", "initial"]
    assert commits[0].author == "t"
    assert commits[0].short_sha == commits[0].sha[:8]
    assert "bump a" in backend.show(commits[0].sha)
    assert "+a2" in backend.diff("HEAD~1", "HEAD", paths=["a.txt"])
    # path-scoped log
    assert len(backend.log(10, paths=["a.txt"])) == 2


@pytest.mark.unit
def test_revert_creates_inverse_commit(repo: Path, backend: GitBackend) -> None:
    (repo / "a.txt").write_text("bad\n")
    bad_sha = backend.commit([repo / "a.txt"], "test(fixture): bad change")
    revert_sha = backend.revert(bad_sha)
    assert revert_sha != bad_sha
    assert (repo / "a.txt").read_text() == "a\n"


@pytest.mark.unit
def test_checkout_paths_restores_a_subtree(
    repo: Path, backend: GitBackend
) -> None:
    sub = repo / "sub"
    sub.mkdir()
    (sub / "x.txt").write_text("v1\n")
    backend.commit([sub / "x.txt"], "test(fixture): sub v1")
    (sub / "x.txt").write_text("v2\n")
    backend.commit([sub / "x.txt"], "test(fixture): sub v2")
    backend.checkout_paths("HEAD~1", ["sub"])
    assert (sub / "x.txt").read_text() == "v1\n"


@pytest.mark.unit
def test_is_dirty_scopes_to_paths(repo: Path, backend: GitBackend) -> None:
    assert not backend.is_dirty()
    sub = repo / "scoped"
    sub.mkdir()
    (sub / "new.txt").write_text("untracked\n")
    assert backend.is_dirty()
    assert backend.is_dirty(paths=["scoped"])
    assert not backend.is_dirty(paths=["a.txt"])


@pytest.mark.unit
def test_ls_files_at_and_commit_exists(repo: Path, backend: GitBackend) -> None:
    assert backend.ls_files_at("HEAD", "a.txt") == ["a.txt"]
    assert backend.commit_exists("HEAD")
    assert not backend.commit_exists("deadbeef")


@pytest.mark.unit
def test_user_email_reads_git_config(backend: GitBackend) -> None:
    assert backend.user_email() == "t@example.com"


@pytest.mark.unit
def test_relpath_rejects_paths_outside_repo(
    backend: GitBackend, tmp_path: Path
) -> None:
    with pytest.raises(GitBackendError, match="outside the repository"):
        backend.relpath(tmp_path / "elsewhere.txt")
