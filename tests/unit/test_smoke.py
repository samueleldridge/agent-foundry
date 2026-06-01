"""Phase 0 smoke test: the package imports and exposes its expected layout.

Anything heavier belongs in a later phase. See docs/03-development-phases.md.
"""

from __future__ import annotations

import importlib

import pytest


def test_foundry_imports() -> None:
    """`import foundry` must succeed."""
    module = importlib.import_module("foundry")
    assert module is not None


@pytest.mark.parametrize(
    "submodule",
    [
        "foundry.core",
        "foundry.providers",
        "foundry.config",
        "foundry.catalog",
        "foundry.auth",
        "foundry.connections",
        "foundry.cache",
        "foundry.retrieval",
        "foundry.memory",
        "foundry.orchestration",
        "foundry.runtime",
        "foundry.eval",
        "foundry.versioning",
        "foundry.configurator",
        "foundry.api",
        "foundry.observability",
        "foundry.storage",
        "foundry.cli",
        "foundry.security",
    ],
)
def test_submodules_import(submodule: str) -> None:
    """Every Phase 0 submodule is importable as a placeholder."""
    importlib.import_module(submodule)
