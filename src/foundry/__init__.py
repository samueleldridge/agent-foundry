"""Public API surface for agent-foundry.

Phase 6 exposes the library-mode forge surface (docs/62 § Library API):

    from foundry import MetaAgent, ForgeGuardrails

Lazily resolved (PEP 562) so ``import foundry`` stays light and run-time
consumers (e.g. ``foundry.api``) never pull in the dev-time configurator
stack as an import side effect.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from foundry.configurator import (
        ForgeGuardrails,
        ForgeResult,
        MetaAgent,
    )

_CONFIGURATOR_EXPORTS = frozenset(
    {"MetaAgent", "ForgeGuardrails", "ForgeResult"}
)


def __getattr__(name: str) -> Any:
    if name in _CONFIGURATOR_EXPORTS:
        import foundry.configurator as _configurator

        return getattr(_configurator, name)
    raise AttributeError(f"module 'foundry' has no attribute {name!r}")


__all__ = ["ForgeGuardrails", "ForgeResult", "MetaAgent"]
