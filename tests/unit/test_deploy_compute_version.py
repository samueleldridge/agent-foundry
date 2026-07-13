"""`foundry compute-version` determinism (docs/84 § Test expectations 1).

Same project state ⇒ same 16-hex hash — within a process, across processes,
and regardless of runtime state under ``.foundry/`` / caches / hidden files.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from foundry.deploy.compute_version import compute_system_version


def _make_project(root: Path) -> Path:
    project = root / "hello"
    (project / "agents" / "hello_agent").mkdir(parents=True)
    (project / "system.yaml").write_text("name: hello\nagents: [hello_agent]\n")
    (project / "state.yaml").write_text("fields: {}\n")
    (project / "agents" / "hello_agent" / "agent.yaml").write_text(
        "name: hello_agent\n"
    )
    return project


@pytest.mark.unit
def test_same_state_same_hash_within_process(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    first = compute_system_version(project)
    second = compute_system_version(project)
    assert first == second
    assert len(first) == 16
    assert all(c in "0123456789abcdef" for c in first)


@pytest.mark.unit
def test_same_state_same_hash_across_processes(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    in_process = compute_system_version(project)
    script = (
        "from pathlib import Path\n"
        "from foundry.deploy.compute_version import compute_system_version\n"
        f"print(compute_system_version(Path({str(project)!r})))\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert completed.stdout.strip() == in_process


@pytest.mark.unit
def test_changed_file_changes_the_hash(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    before = compute_system_version(project)
    (project / "system.yaml").write_text(
        "name: hello\nagents: [hello_agent]\ndescription: changed\n"
    )
    assert compute_system_version(project) != before


@pytest.mark.unit
def test_renamed_file_changes_the_hash(tmp_path: Path) -> None:
    """Paths are part of the hash (path + NUL + bytes), not just contents."""
    project = _make_project(tmp_path)
    before = compute_system_version(project)
    (project / "state.yaml").rename(project / "state2.yaml")
    assert compute_system_version(project) != before


@pytest.mark.unit
def test_runtime_state_and_hidden_files_do_not_affect_the_hash(
    tmp_path: Path,
) -> None:
    project = _make_project(tmp_path)
    before = compute_system_version(project)

    foundry_state = project / ".foundry"
    foundry_state.mkdir()
    (foundry_state / "audit.jsonl").write_text('{"noise": true}\n')
    (project / "__pycache__").mkdir()
    (project / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\x00\x01")
    (project / ".pytest_cache").mkdir()
    (project / ".pytest_cache" / "CACHEDIR.TAG").write_text("tag")
    (project / ".hidden_notes").write_text("scratch")
    (project / "agents" / ".DS_Store").write_bytes(b"junk")

    assert compute_system_version(project) == before


@pytest.mark.unit
def test_include_git_sha_appends_short_sha_in_a_repo(tmp_path: Path) -> None:
    project = _make_project(tmp_path)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(
        ["git", "-c", "user.email=ci@example.com", "-c", "user.name=ci",
         "commit", "-q", "--allow-empty", "-m", "seed"],
        cwd=tmp_path,
        check=True,
    )
    plain = compute_system_version(project)
    versioned = compute_system_version(project, include_git_sha=True)
    assert versioned.startswith(f"{plain}@")
    assert len(versioned.split("@", 1)[1]) == 7


@pytest.mark.unit
def test_include_git_sha_falls_back_outside_a_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Force "not a repository" even if some ancestor of tmp_path is one.
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "no_such_git_dir"))
    project = _make_project(tmp_path)
    plain = compute_system_version(project)
    assert compute_system_version(project, include_git_sha=True) == plain
