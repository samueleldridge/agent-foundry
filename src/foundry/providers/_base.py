"""ProviderAdapter — abstract base every concrete provider subclasses.

The base owns the cross-cutting plumbing: cost-budget pre-check + post-call
record, retries with backoff, per-attempt timeouts, error classification, and
run_id-threaded logging. Concrete adapters implement three hooks:
``_build_request``, ``_parse_response``, ``_classify_http_error``.

Phase 1 adapters call provider HTTP APIs directly via httpx (docs/11 suggests
a LangChain bridge; the direct-httpx choice is documented in the Phase 1
handoff). ``stream()`` is a thin wrapper over ``generate()`` until real
streaming lands in Phase 3.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, ClassVar

import httpx

from foundry.core import (
    FoundryMessage,
    ModelDelta,
    ModelResponse,
    ResolvedCredentials,
    RetryPolicy,
    Session,
    TextBlock,
)
from foundry.core.errors import (
    ProviderAuthError,
    ProviderConfigError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnexpectedError,
    RunCancelled,
)
from foundry.providers._types import (
    ProviderCapabilities,
    ResolvedModelSettings,
    ToolSchema,
)
from foundry.providers.pricing import estimate_cost, estimate_pre_call_cost

_DEFAULT_TIMEOUT_S = 60.0


@dataclass(frozen=True)
class HttpRequestSpec:
    """What a concrete adapter's _build_request returns."""

    url: str
    headers: dict[str, str]
    body: dict[str, Any]


class ProviderAdapter(ABC):
    """Abstract base class for concrete providers (docs/11 § ProviderAdapter)."""

    name: ClassVar[str]
    default_credentials_env: ClassVar[str]

    def __init__(
        self,
        model: str,
        settings: ResolvedModelSettings,
        credentials: ResolvedCredentials,
        manifest: ProviderCapabilities,
        *,
        retry_policy: RetryPolicy | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.model = model
        self.capabilities = manifest
        self._settings = settings
        self._credentials = credentials
        self._retry_policy = retry_policy or RetryPolicy()
        self._transport = transport

    # --- hooks concrete adapters implement -----------------------------------

    @abstractmethod
    def _build_request(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema],
        settings: ResolvedModelSettings,
    ) -> HttpRequestSpec: ...

    @abstractmethod
    def _parse_response(self, payload: dict[str, Any], latency_ms: int) -> ModelResponse: ...

    def _classify_http_error(self, status: int, payload: dict[str, Any]) -> ProviderError:
        """Default status-code classification; concrete adapters extend for
        provider-specific cases (e.g. content-policy refusals)."""
        message = _provider_message(payload)
        context: dict[str, Any] = {
            "http_status": status,
            "provider_message": message,
            "provider": self.name,
            "model": self.model,
        }
        if status in (401, 403):
            return ProviderAuthError(
                f"{self.name} rejected credentials (HTTP {status}): {message}",
                context=context,
            )
        if status == 429:
            return ProviderRateLimitError(
                f"{self.name} rate limit hit (HTTP 429): {message}", context=context
            )
        if status in (408, 504):
            return ProviderTimeoutError(
                f"{self.name} timed out (HTTP {status}): {message}", context=context
            )
        return ProviderUnexpectedError(
            f"{self.name} call failed (HTTP {status}): {message}", context=context
        )

    # --- public surface -------------------------------------------------------

    async def generate(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema],
        settings: ResolvedModelSettings | None = None,
        session: Session | None = None,
    ) -> ModelResponse:
        resolved = settings or self._settings
        self._check_cancelled(session)
        self._pre_call_budget_check(messages, resolved, session)

        attempt = 0
        while True:
            attempt += 1
            try:
                response = await self._attempt(messages, tools, resolved)
                break
            except ProviderError as exc:
                if not exc.retryable or attempt >= self._retry_policy.max_attempts:
                    raise
                self._log(
                    session,
                    "provider.retry",
                    attempt=attempt,
                    error_class=type(exc).__name__,
                )
                await asyncio.sleep(self._backoff_delay(attempt))
                self._check_cancelled(session)

        if session is not None and session.cost_budget is not None:
            actual = response.cost_estimate_usd
            if actual is not None:
                session.cost_budget.record(actual)
        return response

    async def stream(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema],
        settings: ResolvedModelSettings | None = None,
        session: Session | None = None,
    ) -> AsyncIterator[ModelDelta]:
        """Phase 1: synthesised single-delta stream over generate().
        Native per-provider streaming lands with Phase 3's --stream."""
        response = await self.generate(messages, tools, settings, session)
        text = "".join(
            b.text for b in response.message.content if isinstance(b, TextBlock)
        )
        yield ModelDelta(
            content_block_index=0,
            delta=TextBlock(text=text),
            stop_reason=response.stop_reason,
            usage=response.usage,
        )

    # --- internals -------------------------------------------------------------

    async def _attempt(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema],
        settings: ResolvedModelSettings,
    ) -> ModelResponse:
        spec = self._build_request(messages, tools, settings)
        timeout = settings.timeout_s or _DEFAULT_TIMEOUT_S
        started = time.monotonic()
        try:
            async with httpx.AsyncClient(
                transport=self._transport, timeout=timeout
            ) as client:
                http_response = await client.post(
                    spec.url, headers=spec.headers, json=spec.body
                )
        except httpx.TimeoutException as exc:
            raise ProviderTimeoutError(
                f"{self.name} call exceeded {timeout}s",
                context={"provider": self.name, "model": self.model,
                         "timeout_s": timeout},
                cause=exc,
            ) from exc
        except httpx.HTTPError as exc:
            raise ProviderUnexpectedError(
                f"{self.name} transport error: {exc}",
                context={"provider": self.name, "model": self.model},
                cause=exc,
            ) from exc

        latency_ms = int((time.monotonic() - started) * 1000)
        payload = _safe_json(http_response)
        if http_response.status_code >= 400:
            raise self._classify_http_error(http_response.status_code, payload)
        try:
            response = self._parse_response(payload, latency_ms)
        except ProviderError:
            raise
        except Exception as exc:
            raise ProviderUnexpectedError(
                f"{self.name} returned an unparseable response: {exc}",
                context={"provider": self.name, "model": self.model},
                cause=exc,
            ) from exc
        if response.cost_estimate_usd is None:
            response = response.model_copy(
                update={"cost_estimate_usd": estimate_cost(self.capabilities, response.usage)}
            )
        return response

    def _pre_call_budget_check(
        self,
        messages: list[FoundryMessage],
        settings: ResolvedModelSettings,
        session: Session | None,
    ) -> None:
        if session is None or session.cost_budget is None:
            return
        estimate = estimate_pre_call_cost(self.capabilities, messages, settings)
        self._log(
            session,
            "provider.budget_check",
            estimated_usd=str(estimate),
            remaining_usd=str(session.cost_budget.remaining_usd()),
        )
        session.cost_budget.check(estimate)

    def _check_cancelled(self, session: Session | None) -> None:
        if session is not None and session.cancel_token.cancelled():
            raise RunCancelled(
                "run cancelled before provider call",
                context={"reason": session.cancel_token.reason},
            )

    def _backoff_delay(self, attempt: int) -> float:
        policy = self._retry_policy
        if policy.backoff.value == "constant":
            delay = policy.initial_delay_s
        elif policy.backoff.value == "linear":
            delay = policy.initial_delay_s * attempt
        else:  # exponential
            delay = policy.initial_delay_s * (2 ** (attempt - 1))
        delay = min(delay, policy.max_delay_s)
        if policy.jitter:
            delay *= 0.5 + random.random() / 2
        return delay

    def _log(self, session: Session | None, event: str, **kwargs: Any) -> None:
        if session is not None and session.logger is not None:
            session.logger.info(
                event, provider=self.name, model=self.model, **kwargs
            )


def text_of_message(provider: str, message: FoundryMessage) -> str:
    """Concatenated text content of a text-only message. Used where the wire
    format is a plain string (e.g. Anthropic's `system`); messages carrying
    tool blocks go through the adapter's block serialiser instead."""
    parts: list[str] = []
    for block in message.content:
        if isinstance(block, TextBlock):
            parts.append(block.text)
        else:
            raise ProviderConfigError(
                f"{provider} adapter expected a text-only message here; got a "
                f"{getattr(block, 'type', '?')!r} block",
                context={"block_type": getattr(block, "type", "?")},
            )
    return "\n".join(parts)


def _safe_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except json.JSONDecodeError:
        return {"raw_text": response.text[:2000]}
    if isinstance(payload, dict):
        return payload
    return {"raw": payload}


def _provider_message(payload: dict[str, Any]) -> str:
    err = payload.get("error")
    if isinstance(err, dict):
        return str(err.get("message", err))
    if err is not None:
        return str(err)
    return str(payload)[:500]


__all__ = ["HttpRequestSpec", "ProviderAdapter", "text_of_message"]
