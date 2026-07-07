"""Provider registry: name -> ProviderAdapter class + resolve() factory.

Registration happens at import time in each concrete provider module;
``foundry.providers.__init__`` imports every concrete module so the registry
is populated at startup. See docs/11 § Registry and factory.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

import httpx

from foundry.core import CredentialsRef, ResolvedCredentials, RetryPolicy
from foundry.core.errors import ProviderAuthError, ProviderConfigError
from foundry.providers._base import ProviderAdapter
from foundry.providers._manifests import all_capabilities, load_capabilities
from foundry.providers._types import ModelBinding

_ADAPTERS: dict[str, type[ProviderAdapter]] = {}


class SecretsResolver(Protocol):
    """Structural stand-in for foundry.config.secrets.SecretsProvider —
    providers must not import foundry.config (layer direction)."""

    def resolve(self, ref: CredentialsRef | None) -> ResolvedCredentials: ...


def register_provider(
    name: str,
) -> Callable[[type[ProviderAdapter]], type[ProviderAdapter]]:
    def _decorator(cls: type[ProviderAdapter]) -> type[ProviderAdapter]:
        if name in _ADAPTERS:
            raise RuntimeError(f"Provider already registered: {name}")
        _ADAPTERS[name] = cls
        return cls

    return _decorator


def available_providers() -> list[str]:
    return sorted(_ADAPTERS)


def check_capabilities_required(binding: ModelBinding) -> None:
    """Compile-time capability check (docs/11 § Capability-required checking).

    Raises:
        ProviderConfigError: a declared capability is not supported by the
            bound provider+model; the error names which providers/models DO
            support it.
    """
    capabilities = load_capabilities(binding.provider, binding.model)
    for required in binding.capabilities_required:
        if capabilities.supports(required):
            continue
        supporting = sorted(
            f"{c.provider}/{c.model}"
            for c in all_capabilities()
            if c.supports(required)
        )
        raise ProviderConfigError(
            f"model binding requires capability {required.value!r} which "
            f"{binding.provider}/{binding.model} does not support; "
            f"supported by: {', '.join(supporting) or '(no known model)'}",
            context={
                "capability": required.value,
                "provider": binding.provider,
                "model": binding.model,
                "supported_by": supporting,
            },
        )


def resolve(
    binding: ModelBinding,
    secrets: SecretsResolver,
    *,
    retry_policy: RetryPolicy | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> ProviderAdapter:
    """Resolve a ModelBinding into a constructed provider adapter.

    Raises:
        ProviderConfigError: unknown provider / unknown model / unsupported
            declared capability.
        ProviderAuthError: credentials ref unresolvable.
    """
    try:
        cls = _ADAPTERS[binding.provider]
    except KeyError:
        available = ", ".join(available_providers())
        raise ProviderConfigError(
            f"unknown provider {binding.provider!r}; available: {available}",
            context={
                "provider": binding.provider,
                "available": available_providers(),
            },
        ) from None

    manifest = load_capabilities(binding.provider, binding.model)
    check_capabilities_required(binding)

    ref = binding.credentials_ref or CredentialsRef(
        kind="env", value=cls.default_credentials_env
    )
    try:
        credentials = secrets.resolve(ref)
    except ProviderAuthError:
        raise
    except Exception as exc:
        raise ProviderAuthError(
            f"could not resolve credentials for provider {binding.provider!r}: {exc}",
            context={"provider": binding.provider, "credentials_kind": ref.kind},
            cause=exc,
        ) from exc

    return cls(
        model=binding.model,
        settings=binding.settings,
        credentials=credentials,
        manifest=manifest,
        retry_policy=retry_policy,
        transport=transport,
    )


__all__ = [
    "SecretsResolver",
    "available_providers",
    "check_capabilities_required",
    "register_provider",
    "resolve",
]
