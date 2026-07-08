"""Embedder registry: provider name → adapter class + capabilities table.

Same registration pattern as the generation-side provider registry
(docs/11 § Registry and factory), plus a credentials-free capabilities
lookup — the compile-time dimension check must run without secrets.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import httpx

from foundry.core import CredentialsRef, EmbedderCapabilities, ResolvedCredentials
from foundry.core.errors import EmbedderAuthError, EmbedderConfigError
from foundry.providers._registry import SecretsResolver
from foundry.providers.embedders._base import EmbedderAdapter
from foundry.providers.embedders._types import EmbedderBinding

_ADAPTERS: dict[str, type[EmbedderAdapter]] = {}
_MODELS: dict[str, dict[str, EmbedderCapabilities]] = {}


def register_embedder(
    name: str, models: dict[str, EmbedderCapabilities]
) -> Callable[[type[EmbedderAdapter]], type[EmbedderAdapter]]:
    def _decorator(cls: type[EmbedderAdapter]) -> type[EmbedderAdapter]:
        if name in _ADAPTERS:
            raise RuntimeError(f"Embedder provider already registered: {name}")
        _ADAPTERS[name] = cls
        _MODELS[name] = models
        return cls

    return _decorator


def available_embedders() -> list[str]:
    return sorted(_ADAPTERS)


def embedder_capabilities(provider: str, model: str) -> EmbedderCapabilities:
    """Credentials-free capabilities lookup — the compile-time surface the
    dimension-match check runs against (docs/24 § Dimension compatibility).

    Raises:
        EmbedderConfigError: unknown provider or unknown model, naming what
            IS available.
    """
    models = _MODELS.get(provider)
    if models is None:
        raise EmbedderConfigError(
            f"unknown embedder provider {provider!r}; "
            f"available: {', '.join(available_embedders())}",
            context={"provider": provider, "available": available_embedders()},
        )
    capabilities = models.get(model)
    if capabilities is None:
        raise EmbedderConfigError(
            f"unknown embedder model {model!r} for provider {provider!r}; "
            f"known models: {', '.join(sorted(models))}",
            context={"provider": provider, "model": model,
                     "known_models": sorted(models)},
        )
    return capabilities


class _EnvDefaultResolver:
    """Fallback SecretsResolver when the caller passes none (one-shot CLI /
    REPL ergonomics, e.g. the manual smoke test). Runtime paths thread the
    project's real SecretsProvider instead."""

    def resolve(self, ref: CredentialsRef | None) -> ResolvedCredentials:
        if ref is None or ref.kind == "default":
            return ResolvedCredentials(kind="default", secret=None)
        if ref.kind == "env":
            return ResolvedCredentials(
                kind="env", secret=os.environ.get(ref.value or "")
            )
        raise EmbedderAuthError(
            f"credentials_ref kind {ref.kind!r} needs a real SecretsProvider "
            "(env and default only in the fallback resolver)",
            context={"kind": ref.kind},
        )


def load_embedder(
    binding: EmbedderBinding,
    secrets: SecretsResolver | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> EmbedderAdapter:
    """Resolve an EmbedderBinding into a constructed adapter.

    Raises:
        EmbedderConfigError: unknown provider / unknown model.
        EmbedderAuthError: credentials ref unresolvable.
    """
    capabilities = embedder_capabilities(binding.provider, binding.model)
    cls = _ADAPTERS[binding.provider]

    resolver: SecretsResolver = secrets or _EnvDefaultResolver()
    ref = binding.credentials_ref or CredentialsRef(
        kind="env", value=cls.default_credentials_env
    )
    try:
        credentials = resolver.resolve(ref)
    except EmbedderAuthError:
        raise
    except Exception as exc:
        raise EmbedderAuthError(
            f"could not resolve credentials for embedder "
            f"{binding.provider!r}: {exc}",
            context={"provider": binding.provider, "credentials_kind": ref.kind},
            cause=exc,
        ) from exc

    return cls(
        model=binding.model,
        capabilities=capabilities,
        credentials=credentials,
        settings=binding.settings,
        transport=transport,
    )


__all__ = [
    "available_embedders",
    "embedder_capabilities",
    "load_embedder",
    "register_embedder",
]
