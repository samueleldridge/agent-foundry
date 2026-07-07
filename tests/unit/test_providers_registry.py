"""Provider registry + capabilities tests (docs/11 § Test expectations)."""

from __future__ import annotations

import pytest

from foundry.core import CredentialsRef, ResolvedCredentials
from foundry.core.errors import ProviderConfigError
from foundry.providers import (
    AnthropicProvider,
    CapabilityName,
    ModelBinding,
    OpenAIProvider,
    available_providers,
    check_capabilities_required,
    load_capabilities,
    resolve,
)


class FakeSecrets:
    def resolve(self, ref: CredentialsRef | None) -> ResolvedCredentials:
        return ResolvedCredentials(kind="env", secret="fake-key-for-tests")


@pytest.mark.unit
def test_available_providers_lists_anthropic_and_openai() -> None:
    assert available_providers() == ["anthropic", "openai"]


@pytest.mark.unit
def test_unknown_provider_raises_structured_error() -> None:
    binding = ModelBinding(provider="foo", model="whatever")
    with pytest.raises(ProviderConfigError) as excinfo:
        resolve(binding, FakeSecrets())
    assert str(excinfo.value) == "unknown provider 'foo'; available: anthropic, openai"
    assert excinfo.value.context["available"] == ["anthropic", "openai"]


@pytest.mark.unit
def test_unknown_model_raises_with_known_models() -> None:
    binding = ModelBinding(provider="anthropic", model="claude-nonexistent")
    with pytest.raises(ProviderConfigError) as excinfo:
        resolve(binding, FakeSecrets())
    assert "unknown model" in str(excinfo.value)
    assert "claude-haiku-4-5" in excinfo.value.context["known_models"]


@pytest.mark.unit
def test_resolve_constructs_the_right_adapter() -> None:
    anthropic = resolve(
        ModelBinding(provider="anthropic", model="claude-haiku-4-5"), FakeSecrets()
    )
    openai = resolve(ModelBinding(provider="openai", model="gpt-4o"), FakeSecrets())
    assert isinstance(anthropic, AnthropicProvider)
    assert isinstance(openai, OpenAIProvider)
    assert anthropic.capabilities.provider == "anthropic"
    assert openai.capabilities.provider == "openai"


@pytest.mark.unit
def test_capabilities_introspection() -> None:
    caps = load_capabilities("anthropic", "claude-sonnet-4-5")
    assert caps.supports(CapabilityName.CACHE_CONTROL)
    assert caps.supports(CapabilityName.TOOL_USE)
    assert not caps.supports(CapabilityName.REASONING_EFFORT)

    o_caps = load_capabilities("openai", "o3-mini")
    assert o_caps.supports(CapabilityName.REASONING_EFFORT)
    assert not o_caps.supports(CapabilityName.VISION)


@pytest.mark.unit
def test_capability_required_check_passes_when_supported() -> None:
    binding = ModelBinding(
        provider="anthropic",
        model="claude-sonnet-4-5",
        capabilities_required=[CapabilityName.CACHE_CONTROL],
    )
    check_capabilities_required(binding)  # must not raise


@pytest.mark.unit
def test_capability_required_check_fails_with_supporting_models_hint() -> None:
    binding = ModelBinding(
        provider="openai",
        model="gpt-4o",
        capabilities_required=[CapabilityName.EXTENDED_THINKING],
    )
    with pytest.raises(ProviderConfigError) as excinfo:
        check_capabilities_required(binding)
    message = str(excinfo.value)
    assert "extended_thinking" in message
    assert "anthropic/claude-sonnet-4-5" in excinfo.value.context["supported_by"]
