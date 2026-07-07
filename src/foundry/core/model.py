"""ModelResponse, ModelDelta, StopReason, TokenUsage.

These are the foundry-native shapes returned from every provider call. They
are deliberately provider-agnostic — provider-specific fields land in
``raw_provider_response`` as an opaque dict (see docs/10 § ``ModelResponse``).
"""

from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from foundry.core.messages import FoundryMessage, TextBlock


class StopReason(StrEnum):
    END_TURN = "end_turn"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    TOOL_USE = "tool_use"
    ERROR = "error"
    FILTERED = "filtered"


class TokenUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input_tokens: int = 0
    output_tokens: int = 0
    cached_read_tokens: int = 0
    cached_write_tokens: int = 0
    reasoning_tokens: int = 0
    """Reasoning tokens (OpenAI o-series, future Anthropic extended thinking).
    Distinct from output_tokens — billed separately on most vendors. Defaults
    to 0 for models without reasoning."""


class ToolUseBlockDelta(BaseModel):
    """Partial tool-use chunk during streaming. Input is partial JSON text;
    the adapter accumulates and parses only on block-close."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    input_partial_json: str = ""


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: FoundryMessage
    stop_reason: StopReason
    usage: TokenUsage
    model: str
    provider: str
    latency_ms: int
    cost_estimate_usd: Decimal | None = None
    raw_provider_response: dict[str, Any] | None = None
    """Opaque passthrough for provider-specific fields. Never depended on by
    non-provider code."""


class ModelDelta(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_block_index: int
    delta: TextBlock | ToolUseBlockDelta | None = None
    stop_reason: StopReason | None = None
    usage: TokenUsage | None = None
    raw_chunk: dict[str, Any] | None = Field(default=None, repr=False)


__all__ = [
    "ModelDelta",
    "ModelResponse",
    "StopReason",
    "TokenUsage",
    "ToolUseBlockDelta",
]
