"""Model manifest loading: provider+model -> ProviderCapabilities.

Manifests are JSON files shipped with the foundry at
``src/foundry/providers/manifests/<provider>.json`` (docs/11 § Model manifest).
A user-override file (``~/.foundry/model_overrides.json``) is deferred to a
later phase.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

from foundry.core.errors import ProviderConfigError
from foundry.providers._types import ProviderCapabilities

_MANIFEST_DIR = Path(__file__).parent / "manifests"


@cache
def _load_manifest(provider: str) -> dict[str, ProviderCapabilities]:
    path = _MANIFEST_DIR / f"{provider}.json"
    if not path.exists():
        return {}
    raw = json.loads(path.read_text())
    models = raw.get(provider, {})
    return {
        model: ProviderCapabilities(provider=provider, model=model, **entry)
        for model, entry in models.items()
    }


def load_capabilities(provider: str, model: str) -> ProviderCapabilities:
    """Resolve the capabilities record for a provider+model pair.

    Raises:
        ProviderConfigError: the model is not in the provider's manifest.
    """
    manifest = _load_manifest(provider)
    if model not in manifest:
        raise ProviderConfigError(
            f"unknown model {model!r} for provider {provider!r}; "
            f"known models: {', '.join(sorted(manifest)) or '(none)'}",
            context={
                "provider": provider,
                "model": model,
                "known_models": sorted(manifest),
            },
        )
    return manifest[model]


def all_capabilities() -> list[ProviderCapabilities]:
    """Every capabilities record across all shipped manifests. Used to build
    'these providers/models DO support it' hints in capability errors."""
    out: list[ProviderCapabilities] = []
    for path in sorted(_MANIFEST_DIR.glob("*.json")):
        out.extend(_load_manifest(path.stem).values())
    return out


__all__ = ["all_capabilities", "load_capabilities"]
