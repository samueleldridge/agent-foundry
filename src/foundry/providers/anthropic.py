"""Anthropic provider adapter (Messages API via httpx).

This module (plus tests) is the only place allowed to import the ``anthropic``
SDK / ``langchain_anthropic``. Phase 1 needs neither: the adapter calls the
Messages API directly over httpx, which keeps the surface minimal and
mock-friendly. See docs/11 § Anthropic for the capability translation table.
"""

from __future__ import annotations

from typing import Any, ClassVar

from foundry.core import (
    FoundryMessage,
    MessageRole,
    ModelResponse,
    StopReason,
    TextBlock,
    TokenUsage,
)
from foundry.core.errors import ProviderConfigError
from foundry.providers._base import HttpRequestSpec, ProviderAdapter, text_of_message
from foundry.providers._registry import register_provider
from foundry.providers._types import ResolvedModelSettings, ToolSchema

_API_URL = "https://api.anthropic.com/v1/messages"
_API_VERSION = "2023-06-01"
_DEFAULT_MAX_TOKENS = 4096

_STOP_REASON_MAP = {
    "end_turn": StopReason.END_TURN,
    "max_tokens": StopReason.MAX_TOKENS,
    "stop_sequence": StopReason.STOP_SEQUENCE,
    "tool_use": StopReason.TOOL_USE,
    "refusal": StopReason.FILTERED,
}


@register_provider("anthropic")
class AnthropicProvider(ProviderAdapter):
    name: ClassVar[str] = "anthropic"
    default_credentials_env: ClassVar[str] = "ANTHROPIC_API_KEY"

    def _build_request(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema],
        settings: ResolvedModelSettings,
    ) -> HttpRequestSpec:
        if tools:
            raise ProviderConfigError(
                "tool dispatch lands in Phase 2a; Phase 1 providers accept no tools",
                context={"provider": self.name},
            )
        system_parts: list[str] = []
        chat_messages: list[dict[str, Any]] = []
        for message in messages:
            text = text_of_message(self.name, message)
            if message.role is MessageRole.SYSTEM:
                system_parts.append(text)
            elif message.role in (MessageRole.USER, MessageRole.ASSISTANT):
                chat_messages.append(
                    {
                        "role": message.role.value,
                        "content": [{"type": "text", "text": text}],
                    }
                )
            else:
                raise ProviderConfigError(
                    f"unsupported role for Phase 1 anthropic adapter: {message.role}",
                    context={"provider": self.name, "role": message.role.value},
                )

        body: dict[str, Any] = {
            "model": self.model,
            "max_tokens": settings.max_tokens
            or min(_DEFAULT_MAX_TOKENS, self.capabilities.max_output_tokens),
            "messages": chat_messages,
        }
        if system_parts:
            body["system"] = "\n\n".join(system_parts)
        if settings.temperature is not None:
            body["temperature"] = settings.temperature
        if settings.top_p is not None:
            body["top_p"] = settings.top_p
        if settings.stop_sequences:
            body["stop_sequences"] = settings.stop_sequences
        if settings.thinking_budget_tokens is not None:
            body["thinking"] = {
                "type": "enabled",
                "budget_tokens": settings.thinking_budget_tokens,
            }

        return HttpRequestSpec(
            url=_API_URL,
            headers={
                "x-api-key": self._credentials.secret or "",
                "anthropic-version": _API_VERSION,
            },
            body=body,
        )

    def _parse_response(
        self, payload: dict[str, Any], latency_ms: int
    ) -> ModelResponse:
        blocks = [
            TextBlock(text=str(b.get("text", "")))
            for b in payload.get("content", [])
            if b.get("type") == "text"
        ]
        usage_raw = payload.get("usage", {})
        usage = TokenUsage(
            input_tokens=int(usage_raw.get("input_tokens", 0)),
            output_tokens=int(usage_raw.get("output_tokens", 0)),
            cached_read_tokens=int(usage_raw.get("cache_read_input_tokens") or 0),
            cached_write_tokens=int(
                usage_raw.get("cache_creation_input_tokens") or 0
            ),
            reasoning_tokens=0,
        )
        stop_reason = _STOP_REASON_MAP.get(
            str(payload.get("stop_reason")), StopReason.END_TURN
        )
        return ModelResponse(
            message=FoundryMessage(
                role=MessageRole.ASSISTANT, content=list(blocks)
            ),
            stop_reason=stop_reason,
            usage=usage,
            model=str(payload.get("model", self.model)),
            provider=self.name,
            latency_ms=latency_ms,
            raw_provider_response=payload,
        )


__all__ = ["AnthropicProvider"]
