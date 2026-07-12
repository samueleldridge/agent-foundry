"""Provider abstraction — the single place the foundry knows about LLM vendors.

Public surface: the ``Provider`` protocol, ``ModelBinding`` / ``ModelSettings``
/ ``ProviderCapabilities`` types, and the registry (``resolve`` /
``available_providers``). Concrete adapter modules are imported here so the
registry is populated at startup (docs/11 § Registry and factory).

Bedrock / Azure / Vertex remain unregistered stubs in Phase 1; the registry
lists exactly ``anthropic`` and ``openai``.
"""

from __future__ import annotations

from foundry.providers._base import HttpRequestSpec, ProviderAdapter
from foundry.providers._manifests import all_capabilities, load_capabilities
from foundry.providers._registry import (
    SecretsResolver,
    available_providers,
    check_capabilities_required,
    register_provider,
    resolve,
)
from foundry.providers._types import (
    CacheControlMode,
    CapabilityName,
    ModelBinding,
    ModelPricing,
    ModelSettings,
    Provider,
    ProviderCapabilities,
    ReasoningEffort,
    ResolvedModelSettings,
    ResponseFormat,
    ToolSchema,
)
from foundry.providers.anthropic import AnthropicProvider
from foundry.providers.openai import OpenAIProvider
from foundry.providers.pricing import estimate_cost, estimate_pre_call_cost
from foundry.providers.rate_limit import (
    InProcessTokenBucket,
    RateLimiter,
    RedisTokenBucket,
    build_rate_limiter,
    default_rate_limiter,
    reset_default_rate_limiter,
)

__all__ = [
    "AnthropicProvider",
    "CacheControlMode",
    "CapabilityName",
    "HttpRequestSpec",
    "InProcessTokenBucket",
    "ModelBinding",
    "ModelPricing",
    "ModelSettings",
    "OpenAIProvider",
    "Provider",
    "ProviderAdapter",
    "ProviderCapabilities",
    "RateLimiter",
    "ReasoningEffort",
    "RedisTokenBucket",
    "ResolvedModelSettings",
    "ResponseFormat",
    "SecretsResolver",
    "ToolSchema",
    "all_capabilities",
    "available_providers",
    "build_rate_limiter",
    "check_capabilities_required",
    "default_rate_limiter",
    "estimate_cost",
    "estimate_pre_call_cost",
    "load_capabilities",
    "register_provider",
    "reset_default_rate_limiter",
    "resolve",
]
