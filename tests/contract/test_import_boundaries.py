"""Import-boundary contract tests (docs/10 § Enforcement, docs/11 § Contract).

The ruff config is the primary enforcement; these tests pin the behaviour so
a future ruff-config edit that silently loosens the boundary fails CI.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src" / "foundry"

# module-name -> files allowed to import it (repo-relative posix paths)
_BANNED = {
    "langgraph": {
        "src/foundry/runtime/langgraph_adapter.py",
        "src/foundry/runtime/_langgraph_types.py",
    },
    "langchain_core": {
        "src/foundry/runtime/langgraph_adapter.py",
        "src/foundry/runtime/_langgraph_types.py",
    },
    "langchain_anthropic": {"src/foundry/providers/anthropic.py"},
    "langchain_openai": {"src/foundry/providers/openai.py"},
    "anthropic": {"src/foundry/providers/anthropic.py"},
    "openai": {"src/foundry/providers/openai.py"},
}

_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE
)

# Foundry-internal boundaries: subtree -> module prefixes it must not
# import (docs/01 rules 4 + 5; src/foundry/api/ruff.toml is the primary
# enforcement — this test pins the behaviour against a silent loosening).
_INTERNAL_BANNED = {
    "src/foundry/api/": ("foundry.studio", "foundry.configurator"),
}

_DOTTED_IMPORT_RE = re.compile(
    r"^\s*(?:import|from)\s+([A-Za-z_][A-Za-z0-9_.]*)", re.MULTILINE
)


@pytest.mark.contract
def test_no_banned_imports_outside_allowlisted_files() -> None:
    repo_root = SRC.parents[1]
    violations: list[str] = []
    for py_file in SRC.rglob("*.py"):
        rel = py_file.relative_to(repo_root).as_posix()
        for match in _IMPORT_RE.finditer(py_file.read_text()):
            module = match.group(1)
            allowed = _BANNED.get(module)
            if allowed is not None and rel not in allowed:
                violations.append(f"{rel}: imports {module}")
    assert violations == []


@pytest.mark.contract
def test_api_never_imports_studio_or_configurator() -> None:
    """docs/01 rule 5 (api ⊬ studio) + rule 4 (api ⊬ configurator):
    the run-time serving layer stays deployable without the dev-time
    modules (docs/72 § Architecture)."""
    repo_root = SRC.parents[1]
    violations: list[str] = []
    for py_file in SRC.rglob("*.py"):
        rel = py_file.relative_to(repo_root).as_posix()
        banned = next(
            (
                prefixes
                for subtree, prefixes in _INTERNAL_BANNED.items()
                if rel.startswith(subtree)
            ),
            None,
        )
        if banned is None:
            continue
        for match in _DOTTED_IMPORT_RE.finditer(py_file.read_text()):
            module = match.group(1)
            if module.startswith(banned):
                violations.append(f"{rel}: imports {module}")
    assert violations == []


@pytest.mark.contract
def test_ruff_flags_api_importing_studio() -> None:
    """The nested src/foundry/api/ruff.toml must actually FIRE on an
    api → studio import (Phase 10a exit gate: import boundary)."""
    victim = SRC / "api" / "_boundary_probe_tmp.py"
    victim.write_text(
        "from foundry.studio import create_studio_app  # deliberate\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(victim)],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        victim.unlink()
    assert result.returncode != 0
    assert "TID251" in result.stdout
    assert "foundry.studio" in result.stdout


@pytest.mark.contract
def test_ruff_flags_a_deliberate_boundary_violation(tmp_path: Path) -> None:
    """The lint must actually FIRE on a violation, not just pass when clean."""
    victim = SRC / "core" / "_boundary_probe_tmp.py"
    victim.write_text("import langgraph  # deliberate violation\n")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(victim)],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        victim.unlink()
    assert result.returncode != 0
    assert "TID251" in result.stdout


@pytest.mark.contract
def test_core_public_api_has_no_third_party_types() -> None:
    """Nothing re-exported from foundry.core is a langgraph/langchain/
    provider-SDK object (docs/10 § Test expectations, contract 2)."""
    import foundry.core as core

    banned_prefixes = ("langgraph", "langchain", "anthropic", "openai")
    for name in core.__all__:
        obj = getattr(core, name)
        module = getattr(obj, "__module__", "") or ""
        assert not module.startswith(banned_prefixes), f"{name} from {module}"
