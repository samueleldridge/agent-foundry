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
import os
import random
import re
import time
from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Callable
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
from foundry.providers.rate_limit import RateLimiter, default_rate_limiter

_DEFAULT_TIMEOUT_S = 60.0

# --- rate-limit backoff schedule (docs/11 § Retry policy) --------------------
#
# Rate-limit errors (HTTP 429) get their OWN retry schedule, separate from
# the RetryPolicy that governs other retryables: TPM/RPM limits clear on
# the provider's clock, not ours, so the right posture is patient —
# exponential full-jitter backoff up to a minute, honouring the provider's
# Retry-After signal when it sends one. Env knobs (documented defaults):
#
#   FOUNDRY_RATE_LIMIT_MAX_ATTEMPTS   total attempts on 429s (default 8)
#   FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S  per-sleep cap in seconds (default 60)

_RATE_LIMIT_MAX_ATTEMPTS_DEFAULT = 8
_RATE_LIMIT_MAX_BACKOFF_S_DEFAULT = 60.0
_RATE_LIMIT_BASE_DELAY_S = 1.0
_CANCEL_POLL_SLICE_S = 0.2
"""Backoff sleeps are split into slices this long so a session cancel is
observed DURING the wait, not after it."""

_RETRY_HINT_RE = re.compile(
    r"try again in\s+([0-9]+(?:\.[0-9]+)?)\s*(ms|milliseconds?|s|sec|seconds?)\b",
    re.IGNORECASE,
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value >= 1 else default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def rate_limit_max_attempts() -> int:
    """FOUNDRY_RATE_LIMIT_MAX_ATTEMPTS (default 8)."""
    return _env_int(
        "FOUNDRY_RATE_LIMIT_MAX_ATTEMPTS", _RATE_LIMIT_MAX_ATTEMPTS_DEFAULT
    )


def rate_limit_max_backoff_s() -> float:
    """FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S (default 60)."""
    return _env_float(
        "FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S", _RATE_LIMIT_MAX_BACKOFF_S_DEFAULT
    )


def parse_retry_after(
    header: str | None, provider_message: str
) -> float | None:
    """The provider's own "come back in N" signal, in seconds.

    Sources, in precedence order: the ``Retry-After`` header (delta-seconds
    form), then the human-readable hint some 429 bodies carry (OpenAI:
    "Please try again in 589ms"). Returns None when neither is present."""
    if header is not None:
        try:
            seconds = float(header.strip())
        except ValueError:
            seconds = -1.0
        if seconds >= 0:
            return seconds
    match = _RETRY_HINT_RE.search(provider_message)
    if match is not None:
        value = float(match.group(1))
        unit = match.group(2).lower()
        return value / 1000.0 if unit.startswith("m") else value
    return None


@dataclass(frozen=True)
class RetryInfo:
    """What the adapter tells observers about one backoff (the
    ``provider.retry`` event payload, pre-run_id/sequence)."""

    attempt: int
    delay_s: float
    error_class: str
    rate_limited: bool
    retry_after_s: float | None


OnRetry = Callable[[RetryInfo], None]


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
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.model = model
        self.capabilities = manifest
        self._settings = settings
        self._credentials = credentials
        self._retry_policy = retry_policy or RetryPolicy()
        self._transport = transport
        self._rate_limiter = rate_limiter
        """Explicit limiter override; None consults the env-configured
        process default (FOUNDRY_RATE_LIMITER, docs/85) per call."""

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
        *,
        on_retry: OnRetry | None = None,
    ) -> ModelResponse:
        resolved = settings or self._settings
        limiter = (
            self._rate_limiter
            if self._rate_limiter is not None
            else default_rate_limiter()
        )
        attempt = 0
        rate_limited_attempts = 0
        while True:
            attempt += 1
            # Per ATTEMPT, not per call: a long backoff must neither hide a
            # cancel nor let an over-budget retry through (docs/11 § Cost-
            # budget integration — the pre-call check guards every attempt).
            self._check_cancelled(session)
            self._pre_call_budget_check(messages, resolved, session)
            if limiter is not None:
                # The docs/85 gate: keyed <provider>:<model>, shared across
                # workers when Redis-backed. Blocks until a permit is
                # granted; cancellation wins over the deferred wait. Each
                # ATTEMPT takes a permit — a 429-retry loop that skipped
                # the gate would hammer an already-limited provider and
                # steal budget from sibling workers.
                await limiter.acquire(
                    f"{self.name}:{self.model}", session=session
                )
            try:
                response = await self._attempt(messages, tools, resolved)
                break
            except ProviderRateLimitError as exc:
                # Patient schedule, separate from other retryables: rate
                # limits clear on the provider's clock. Exponential base 1s
                # x2 with FULL jitter, capped (FOUNDRY_RATE_LIMIT_MAX_
                # BACKOFF_S); Retry-After wins when the provider sent one;
                # attempts capped by FOUNDRY_RATE_LIMIT_MAX_ATTEMPTS.
                rate_limited_attempts += 1
                if rate_limited_attempts >= rate_limit_max_attempts():
                    raise
                retry_after = _retry_after_from(exc.context)
                delay = self._rate_limit_delay(
                    rate_limited_attempts, retry_after
                )
                self._notify_retry(
                    session,
                    on_retry,
                    RetryInfo(
                        attempt=rate_limited_attempts,
                        delay_s=delay,
                        error_class=type(exc).__name__,
                        rate_limited=True,
                        retry_after_s=retry_after,
                    ),
                )
                await self._sleep_cancellable(delay, session)
            except ProviderError as exc:
                if not exc.retryable or attempt >= self._retry_policy.max_attempts:
                    raise
                delay = self._backoff_delay(attempt)
                self._notify_retry(
                    session,
                    on_retry,
                    RetryInfo(
                        attempt=attempt,
                        delay_s=delay,
                        error_class=type(exc).__name__,
                        rate_limited=False,
                        retry_after_s=None,
                    ),
                )
                await self._sleep_cancellable(delay, session)

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
        *,
        on_retry: OnRetry | None = None,
    ) -> AsyncIterator[ModelDelta]:
        """Phase 1: synthesised single-delta stream over generate().
        Native per-provider streaming lands with Phase 3's --stream."""
        response = await self.generate(
            messages, tools, settings, session, on_retry=on_retry
        )
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
            error = self._classify_http_error(
                http_response.status_code, payload
            )
            if (
                isinstance(error, ProviderRateLimitError)
                and "retry_after_s" not in error.context
            ):
                hint = parse_retry_after(
                    http_response.headers.get("retry-after"),
                    _provider_message(payload),
                )
                if hint is not None:
                    error.context["retry_after_s"] = hint
            raise error
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

    def _rate_limit_delay(
        self, rate_limited_attempt: int, retry_after_s: float | None
    ) -> float:
        """The 429 schedule: honour Retry-After exactly when present
        (capped); otherwise FULL jitter over an exponential ceiling —
        random.uniform(0, min(cap, 1s * 2^(n-1)))."""
        cap = rate_limit_max_backoff_s()
        if retry_after_s is not None:
            return min(max(retry_after_s, 0.0), cap)
        ceiling = min(
            cap,
            _RATE_LIMIT_BASE_DELAY_S * (2 ** (rate_limited_attempt - 1)),
        )
        return random.uniform(0.0, ceiling)

    def _notify_retry(
        self, session: Session | None, on_retry: OnRetry | None, info: RetryInfo
    ) -> None:
        self._log(
            session,
            "provider.retry",
            attempt=info.attempt,
            error_class=info.error_class,
            delay_s=round(info.delay_s, 3),
            rate_limited=info.rate_limited,
            retry_after_s=info.retry_after_s,
        )
        if on_retry is not None:
            on_retry(info)

    async def _sleep_cancellable(
        self, delay: float, session: Session | None
    ) -> None:
        """Backoff sleep that observes cancellation DURING the wait: long
        sleeps are split into bounded slices and the session token is
        checked between slices (a 60s backoff must not pin a cancelled
        run for 60s)."""
        if session is None:
            await asyncio.sleep(delay)
            return
        deadline = time.monotonic() + delay
        while True:
            if session.cancel_token.cancelled():
                raise RunCancelled(
                    "run cancelled during provider backoff",
                    context={"reason": session.cancel_token.reason},
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            await asyncio.sleep(min(remaining, _CANCEL_POLL_SLICE_S))

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


def _retry_after_from(context: dict[str, Any]) -> float | None:
    value = context.get("retry_after_s")
    if isinstance(value, int | float):
        return float(value)
    return None


__all__ = [
    "HttpRequestSpec",
    "OnRetry",
    "ProviderAdapter",
    "RetryInfo",
    "parse_retry_after",
    "rate_limit_max_attempts",
    "rate_limit_max_backoff_s",
    "text_of_message",
]
