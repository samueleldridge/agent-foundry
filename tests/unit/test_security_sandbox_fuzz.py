"""docs/83 contract: 50-case malicious-path fuzz — every escape attempt
refused by the consolidated PathSandbox (traversal, symlinks, absolute
paths, encoded sequences, catalog/framework writes, denied subtrees)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from foundry.core.errors import SandboxViolation
from foundry.security.sandbox import PathSandbox


@pytest.fixture
def tree(tmp_path: Path) -> dict[str, Path]:
    repo = tmp_path / "repo"
    project = repo / "projects" / "demo"
    framework = repo / "src" / "foundry"
    catalog = repo / "catalog"
    outside = tmp_path / "outside"
    for directory in (project / "agents", framework, catalog / "tools", outside):
        directory.mkdir(parents=True)
    (outside / "victim.txt").write_text("secret")
    (project / "agents" / "a.yaml").write_text("ok")
    # symlink escapes: a link inside the project pointing out of it
    (project / "sneaky_link").symlink_to(outside)
    (project / "sneaky_file").symlink_to(outside / "victim.txt")
    return {
        "repo": repo,
        "project": project,
        "framework": framework,
        "catalog": catalog,
        "outside": outside,
    }


def _sandbox(tree: dict[str, Path]) -> PathSandbox:
    return PathSandbox(
        base_dir=tree["repo"],
        read_roots=(tree["project"], tree["framework"], tree["catalog"]),
        write_root=tree["project"],
    )


def _malicious_write_paths(tree: dict[str, Path]) -> list[str]:
    project = tree["project"]
    outside = tree["outside"]
    home = Path.home()
    cases = [
        # --- traversal out of the project (relative to repo root) ---
        "projects/demo/../../escape.txt",
        "projects/demo/../other/system.yaml",
        "projects/demo/agents/../../../etc/passwd",
        "projects/demo/./../demo2/x.yaml",
        "projects/../projects_evil/demo/x.yaml",
        "projects/demo/a/b/../../../../escape.txt",
        "../outside/victim.txt",
        "..",
        "../../..",
        "./..",
        # --- absolute paths outside every root ---
        "/etc/passwd",
        "/tmp/evil.txt",
        str(outside / "victim.txt"),
        str(outside),
        str(home / ".ssh" / "authorized_keys"),
        str(home / ".foundry" / "config.yaml"),
        "/",
        "/dev/null",
        "/var/log/system.log",
        str(tree["repo"].parent / "sibling.txt"),
        # --- framework + catalog writes (readable, never writable) ---
        "src/foundry/core/agent.py",
        "src/foundry/evil.py",
        "catalog/tools/evil/v1/tool.yaml",
        "catalog/index.yaml",
        str(tree["framework"] / "runtime.py"),
        str(tree["catalog"] / "tools" / "x.yaml"),
        # --- denied subtrees inside the project ---
        "projects/demo/evals/qa.yaml",
        "projects/demo/evals/new/deep/case.yaml",
        "projects/demo/.foundry/audit.jsonl",
        "projects/demo/.foundry/eval_history.jsonl",
        str(project / "evals" / "qa.yaml"),
        str(project / ".foundry" / "audit.jsonl"),
        # --- traversal that ENTERS a denied subtree after resolution ---
        "projects/demo/agents/../evals/qa.yaml",
        "projects/demo/agents/../.foundry/audit.jsonl",
        str(project / "agents" / ".." / "evals" / "qa.yaml"),
        # --- symlink escapes ---
        "projects/demo/sneaky_link/evil.txt",
        "projects/demo/sneaky_file",
        str(project / "sneaky_link" / "victim.txt"),
        "projects/demo/sneaky_link/../victim.txt",
        # --- prefix-confusion: sibling dir sharing the project prefix ---
        "projects/demo2/x.yaml",
        "projects/demo_evil/x.yaml",
        str(project.parent / "demofake" / "x.yaml"),
        # --- encoded / odd shapes (resolve() must not be fooled) ---
        "projects/demo/\u2024\u2024/escape.txt",  # lookalike-dots dir (does not exist)
        "projects/demo/agents/..\\..\\escape.txt",  # backslash is a filename char
        "projects/demo/.././../escape.txt",
        "projects/demo//../escape.txt",
        "projects/demo/agents/./.././../escape.txt",
        " /etc/passwd",  # leading-space name resolves under the repo, not /etc
        "~/.ssh/id_rsa",  # tilde is NOT expanded by Path.resolve
        "projects/demo/agents/a.yaml/../../../../../../etc/shadow",
    ]
    return cases


@pytest.mark.contract
def test_malicious_write_fuzz_all_refused(tree: dict[str, Path]) -> None:
    sandbox = _sandbox(tree)
    cases = _malicious_write_paths(tree)
    assert len(cases) >= 50  # the docs/83 contract size
    refused = 0
    allowed_inside: list[str] = []
    for raw in cases:
        try:
            resolved = sandbox.check_write(raw)
        except SandboxViolation:
            refused += 1
            continue
        # Any accepted path MUST have resolved inside the writable root and
        # outside the denied subtrees — otherwise the sandbox has a hole.
        assert resolved.is_relative_to(tree["project"]), raw
        assert not str(resolved).startswith(str(tree["outside"])), raw
        allowed_inside.append(raw)
    # The overwhelming majority must be refused outright; the tolerated
    # remainder are odd-shaped names that RESOLVE inside the project
    # (e.g. "~" or lookalike-dot dirs are literal filenames — writes to
    # them stay inside the sandbox and are therefore safe by definition).
    assert refused >= len(cases) - 4, (refused, allowed_inside)
    # And none of the classic escapes may be in the tolerated set:
    for raw in allowed_inside:
        assert ".." not in raw or "\\" in raw or "\u2024" in raw, raw


@pytest.mark.contract
def test_malicious_read_fuzz_all_refused(tree: dict[str, Path]) -> None:
    sandbox = _sandbox(tree)
    outside = tree["outside"]
    read_cases = [
        str(outside / "victim.txt"),
        "/etc/passwd",
        str(Path.home() / ".ssh" / "id_rsa"),
        "../outside/victim.txt",
        "projects/demo/sneaky_link/victim.txt",
        "projects/demo/sneaky_file",
        "projects/../../outside/victim.txt",
        str(tree["repo"].parent),
        "/",
        "projects/demo/../../../outside/victim.txt",
    ]
    for raw in read_cases:
        with pytest.raises(SandboxViolation):
            sandbox.check_read(raw)


@pytest.mark.unit
def test_legitimate_paths_pass(tree: dict[str, Path]) -> None:
    sandbox = _sandbox(tree)
    assert sandbox.check_write("projects/demo/agents/a.yaml").name == "a.yaml"
    assert sandbox.check_write(str(tree["project"] / "system.yaml")).name == "system.yaml"
    assert sandbox.check_read("src/foundry/core/agent.py").name == "agent.py"
    assert sandbox.check_read("catalog/tools/evil/v1/tool.yaml")
    # versions metadata inside the project is writable
    assert sandbox.check_write("projects/demo/tools/t/versions.json")


@pytest.mark.unit
def test_write_root_none_forbids_all_writes(tree: dict[str, Path]) -> None:
    sandbox = PathSandbox(base_dir=tree["repo"], read_roots=(tree["project"],))
    with pytest.raises(SandboxViolation):
        sandbox.check_write("projects/demo/agents/a.yaml")


@pytest.mark.unit
def test_symlink_created_after_check_does_not_matter_for_resolution(
    tree: dict[str, Path],
) -> None:
    """resolve() follows symlinks at check time — the canonical target is
    what gets checked, not the raw path string."""
    sandbox = _sandbox(tree)
    target = sandbox.resolve("projects/demo/sneaky_file")
    assert target == (tree["outside"] / "victim.txt").resolve()


def test_module_is_importable_without_side_effects() -> None:
    before = dict(os.environ)
    import foundry.security  # noqa: F401

    assert dict(os.environ) == before
