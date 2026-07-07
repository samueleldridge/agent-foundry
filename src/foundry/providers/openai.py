"""OpenAI provider adapter (Chat Completions API via httpx).

This module (plus tests) is the only place allowed to import the ``openai``
SDK / ``langchain_openai``. Phase 1 needs neither: the adapter calls the
Chat Completions API directly over httpx. See docs/11 § OpenAI.

Reasoning models (o-series, gpt-5 family — ``reasoning_effort`` capability in
the manifest) use ``max_completion_tokens`` and surface
``usage.completion_tokens_details.reasoning_tokens``, which this adapter
normalises into ``TokenUsage.reasoning_tokens``.
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
from foundry.core.errors import (
    ProviderConfigError,
    ProviderContentPolicyError,
    ProviderError,
)
from foundry.providers._base import HttpRequestSpec, ProviderAdapter, text_of_message
from foundry.providers._registry import register_provider
from foundry.providers._types import ResolvedModelSettings, ToolSchema

_API_URL = "https://api.openai.com/v1/chat/completions"

_FINISH_REASON_MAP = {
    "stop": StopReason.END_TURN,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.FILTERED,
    "tool_calls": StopReason.TOOL_USE,
    "function_call": StopReason.TOOL_USE,
}

_ROLE_MAP = {
    MessageRole.SYSTEM: "system",
    MessageRole.USER: "user",
    MessageRole.ASSISTANT: "assistant",
}


@register_provider("openai")
class OpenAIProvider(ProviderAdapter):
    name: ClassVar[str] = "openai"
    default_credentials_env: ClassVar[str] = "OPENAI_API_KEY"

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
        chat_messages: list[dict[str, Any]] = []
        for message in messages:
            role = _ROLE_MAP.get(message.role)
            if role is None:
                raise ProviderConfigError(
                    f"unsupported role for Phase 1 openai adapter: {message.role}",
                    context={"provider": self.name, "role": message.role.value},
                )
            chat_messages.append(
                {"role": role, "content": text_of_message(self.name, message)}
            )

        body: dict[str, Any] = {"model": self.model, "messages": chat_messages}
        if settings.max_tokens is not None:
            # Reasoning models reject `max_tokens`; they budget completion +
            # hidden reasoning together via `max_completion_tokens`.
            key = (
                "max_completion_tokens"
                if self.capabilities.reasoning_effort
                else "max_tokens"
            )
            body[key] = settings.max_tokens
        if settings.temperature is not None:
            body["temperature"] = settings.temperature
        if settings.top_p is not None:
            body["top_p"] = settings.top_p
        if settings.stop_sequences:
            body["stop"] = settings.stop_sequences
        if settings.seed is not None:
            body["seed"] = settings.seed
        if settings.reasoning_effort is not None:
            body["reasoning_effort"] = settings.reasoning_effort.value
        if settings.response_format is not None:
            if settings.response_format.type == "json_schema":
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "output",
                        "schema": settings.response_format.json_schema or {},
                        "strict": True,
                    },
                }
            else:
                body["response_format"] = {"type": "json_object"}

        return HttpRequestSpec(
            url=_API_URL,
            headers={"Authorization": f"Bearer {self._credentials.secret or ''}"},
            body=body,
        )

    def _parse_response(
        self, payload: dict[str, Any], latency_ms: int
    ) -> ModelResponse:
        choices = payload.get("choices") or []
        first = choices[0] if choices else {}
        content = (first.get("message") or {}).get("content") or ""
        usage_raw = payload.get("usage", {})
        completion_details = usage_raw.get("completion_tokens_details") or {}
        prompt_details = usage_raw.get("prompt_tokens_details") or {}
        # OpenAI's `completion_tokens` INCLUDES hidden reasoning tokens.
        # TokenUsage keeps them distinct (docs/10 § TokenUsage), so subtract:
        # output_tokens = visible completion, reasoning_tokens = hidden.
        # pricing.estimate_cost bills output + reasoning at the output rate,
        # which then equals completion_tokens exactly (no double-billing).
        completion_tokens = int(usage_raw.get("completion_tokens", 0))
        reasoning_tokens = int(completion_details.get("reasoning_tokens") or 0)
        usage = TokenUsage(
            input_tokens=int(usage_raw.get("prompt_tokens", 0)),
            output_tokens=max(0, completion_tokens - reasoning_tokens),
            cached_read_tokens=int(prompt_details.get("cached_tokens") or 0),
            cached_write_tokens=0,
            reasoning_tokens=reasoning_tokens,
        )
        stop_reason = _FINISH_REASON_MAP.get(
            str(first.get("finish_reason")), StopReason.END_TURN
        )
        return ModelResponse(
            message=FoundryMessage(
                role=MessageRole.ASSISTANT, content=[TextBlock(text=str(content))]
            ),
            stop_reason=stop_reason,
            usage=usage,
            model=str(payload.get("model", self.model)),
            provider=self.name,
            latency_ms=latency_ms,
            raw_provider_response=payload,
        )

    def _classify_http_error(
        self, status: int, payload: dict[str, Any]
    ) -> ProviderError:
        err = payload.get("error")
        if status == 400 and isinstance(err, dict) and err.get("code") == "content_filter":
            return ProviderContentPolicyError(
                f"{self.name} refused the request on content-policy grounds: "
                f"{err.get('message', '')}",
                context={
                    "http_status": status,
                    "provider": self.name,
                    "model": self.model,
                    "provider_error_code": "content_filter",
                },
            )
        return super()._classify_http_error(status, payload)


__all__ = ["OpenAIProvider"]
