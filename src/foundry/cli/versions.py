"""`foundry versions` + `foundry diff` (docs/52 § CLI surface).

``versions`` is the single discovery command: recent commits scoped to the
project + per-artifact version state (what exists on disk, what is pinned).
``diff`` is git-diff between two refs scoped to the project subtree.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from foundry.cli._helpers import print_foundry_error, resolve_project_dir
from foundry.config.refs import FoundryRoots, list_versions
from foundry.core.errors import FoundryError
from foundry.versioning.artifacts import list_prompt_versions, prompts_dir
from foundry.versioning.git_backend import GitBackend
from foundry.versioning.refs import parse_artifact_ref


def execute_versions(project: str, *, tool: str | None = None) -> int:
    try:
        project_dir = resolve_project_dir(project)
        backend = GitBackend.discover(project_dir)
        system = _load_raw_system(project_dir)
        roots = FoundryRoots.for_project(project_dir)
        if tool is not None:
            return _print_one_tool(project_dir, system, roots, tool)
        _print_overview(project_dir, backend, system, roots)
        return 0
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2


def execute_diff(
    project: str, ref1: str, ref2: str, *, path: str | None = None
) -> int:
    try:
        project_dir = resolve_project_dir(project)
        backend = GitBackend.discover(project_dir)
        rel = backend.relpath(project_dir)
        scope = f"{rel}/{path.lstrip('/')}" if path else rel
        out = backend.diff(ref1, ref2, paths=[scope])
        print(out if out.strip() else f"(no differences under {scope})")
        return 0
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2


# --- rendering -----------------------------------------------------------------------


def _load_raw_system(project_dir: Path) -> dict[str, object]:
    data = yaml.safe_load((project_dir / "system.yaml").read_text())
    return data if isinstance(data, dict) else {}


def _print_overview(
    project_dir: Path,
    backend: GitBackend,
    system: dict[str, object],
    roots: FoundryRoots,
) -> None:
    name = project_dir.name
    print(f"Project: {name} (branch {backend.current_branch()})")
    rel = backend.relpath(project_dir)
    commits = backend.log(10, paths=[rel])
    print(f"\nRecent commits touching {rel} ({len(commits)}):")
    for c in commits:
        print(f"  {c.short_sha}  {c.date[:10]}  {c.subject}")
    if not commits:
        print("  (none)")

    agents = system.get("agents") or []
    if isinstance(agents, list) and agents:
        print("\nAgents (active prompt pin marked *):")
        for agent in agents:
            versions = list_prompt_versions(prompts_dir(project_dir, str(agent)))
            pinned = _prompt_pin(project_dir, str(agent))
            rendered = ", ".join(
                f"*{v}" if v == pinned else v for v in versions
            )
            print(f"  {agent:<24} prompts: {rendered or '(none)'}")

    tools = system.get("tools") or {}
    if isinstance(tools, dict) and tools:
        print("\nTools (pinned version marked *):")
        for logical, binding in sorted(tools.items()):
            _print_binding_line(logical, binding, roots, kind="tool")

    connections = system.get("connections") or {}
    if isinstance(connections, dict) and connections:
        print("\nConnections:")
        for logical, binding in sorted(connections.items()):
            _print_binding_line(logical, binding, roots, kind="connection")


def _print_binding_line(
    logical: str, binding: object, roots: FoundryRoots, *, kind: str
) -> None:
    if not isinstance(binding, dict):
        return
    ref_str = str(binding.get("ref", ""))
    pinned = str(binding.get("version", ""))
    try:
        ref = parse_artifact_ref(ref_str, default_kind=kind, version=pinned)  # type: ignore[arg-type]
        versions = list_versions(ref.artifact_dir(roots))
    except FoundryError:
        versions = []
    rendered = ", ".join(f"*{v}" if v == pinned else v for v in versions)
    latest_note = ""
    if versions and versions[-1] != pinned:
        latest_note = f"  ({versions[-1]} available, not pinned)"
    print(
        f"  {logical:<24} {ref_str:<32} "
        f"versions: {rendered or pinned}{latest_note}"
    )


def _print_one_tool(
    project_dir: Path,
    system: dict[str, object],
    roots: FoundryRoots,
    tool: str,
) -> int:
    tools = system.get("tools") or {}
    binding = tools.get(tool) if isinstance(tools, dict) else None
    if not isinstance(binding, dict):
        known = sorted(tools) if isinstance(tools, dict) else []
        print(
            f"tool {tool!r} is not bound in {project_dir / 'system.yaml'} "
            f"(known: {', '.join(known) or '(none)'})"
        )
        return 2
    _print_binding_line(tool, binding, roots, kind="tool")
    return 0


def _prompt_pin(project_dir: Path, agent: str) -> str:
    agent_yaml = project_dir / "agents" / agent / "agent.yaml"
    if not agent_yaml.is_file():
        return ""
    data = yaml.safe_load(agent_yaml.read_text())
    prompt = data.get("prompt") if isinstance(data, dict) else None
    return str(prompt.get("version", "")) if isinstance(prompt, dict) else ""


__all__ = ["execute_diff", "execute_versions"]
