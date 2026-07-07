"""Provider-neutral types: ModelBinding, ModelSettings, capabilities, pricing.

These are the types that appear in config schemas (``AgentSpec.model_binding``)
and that every provider adapter programs against. See
docs/11-provider-abstraction.md for the normative spec.
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from foundry.core import CredentialsRef, FoundryMessage, ModelDelta, ModelResponse, Session


class CapabilityName(StrEnum):
    CACHE_CONTROL = "cache_control"
    EXTENDED_THINKING = "extended_thinking"
    REASONING_EFFORT = "reasoning_effort"
    STRUCTURED_OUTPUTS = "structured_outputs"
    VISION = "vision"
    TOOL_USE = "tool_use"
    TOOL_CHOICE = "tool_choice"
    STREAMING = "streaming"
    SEED = "seed"
    PREFILL = "prefill"
    LOGPROBS = "logprobs"
    PDF_INPUT = "pdf_input"


class ModelPricing(BaseModel):
    """Per-1M-token prices in USD. Indicative, not authoritative."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    input_per_1m: Decimal
    output_per_1m: Decimal
    cache_read_per_1m: Decimal = Decimal("0")
    cache_write_per_1m: Decimal = Decimal("0")


class ProviderCapabilities(BaseModel):
    """Static descriptor of what a provider+model combination supports.

    Populated from the per-provider manifest at startup. See
    docs/11 § ProviderCapabilities.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    max_context_tokens: int
    max_output_tokens: int

    cache_control: bool = False
    extended_thinking: bool = False
    reasoning_effort: bool = False
    structured_outputs: bool = False
    vision: bool = False
    tool_use: bool = True
    tool_choice: bool = True
    streaming: bool = True
    seed: bool = False
    prefill: bool = False
    logprobs: bool = False
    pdf_input: bool = False

    pricing: ModelPricing

    def supports(self, name: CapabilityName) -> bool:
        return bool(getattr(self, name.value))


class ResponseFormat(BaseModel):
    """Provider-neutral structured-output request. Requires the
    ``structured_outputs`` capability."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    type: Literal["json", "json_schema"] = "json"
    json_schema: dict[str, Any] | None = Field(default=None, alias="schema")


class ReasoningEffort(StrEnum):
    MINIMAL = "minimal"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CacheControlMode(StrEnum):
    OFF = "off"
    SYSTEM = "system"
    SYSTEM_AND_TOOLS = "system_and_tools"
    AGGRESSIVE = "aggressive"


class ModelSettings(BaseModel):
    """Provider-neutral knobs. Provider-specific knobs go through the
    capabilities system or ``ModelBinding.provider_overrides``."""

    model_config = ConfigDict(extra="forbid")

    temperature: float | None = None
    max_tokens: int | None = None
    top_p: float | None = None
    stop_sequences: list[str] = Field(default_factory=list)
    response_format: ResponseFormat | None = None
    seed: int | None = None
    reasoning_effort: ReasoningEffort | None = None
    thinking_budget_tokens: int | None = None
    cache_control: CacheControlMode | None = None
    timeout_s: float | None = None


# Alias: what a provider receives after binding settings are merged with
# provider_overrides and session-level overrides (docs/11 § ModelSettings).
ResolvedModelSettings = ModelSettings


class ModelBinding(BaseModel):
    """Pins everything needed to locate, configure, and call a model.

    Appears inside ``AgentSpec``; validated against the provider registry
    at compile time.
    """

    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str
    settings: ModelSettings = Field(default_factory=ModelSettings)
    capabilities_required: list[CapabilityName] = Field(default_factory=list)
    provider_overrides: dict[str, Any] = Field(default_factory=dict)
    credentials_ref: CredentialsRef | None = None


class ToolSchema(BaseModel):
    """Provider-neutral tool definition passed into generate/stream."""

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    input_schema: dict[str, Any]


@runtime_checkable
class Provider(Protocol):
    """The narrow interface the rest of the foundry programs against."""

    name: str
    model: str
    capabilities: ProviderCapabilities

    async def generate(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema],
        settings: ResolvedModelSettings,
        session: Session | None = None,
    ) -> ModelResponse: ...

    def stream(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema],
        settings: ResolvedModelSettings,
        session: Session | None = None,
    ) -> Any: ...  # AsyncIterator[ModelDelta]; typed loosely for protocol ergonomics


__all__ = [
    "CacheControlMode",
    "CapabilityName",
    "ModelBinding",
    "ModelDelta",
    "ModelPricing",
    "ModelSettings",
    "Provider",
    "ProviderCapabilities",
    "ReasoningEffort",
    "ResolvedModelSettings",
    "ResponseFormat",
    "ToolSchema",
]
