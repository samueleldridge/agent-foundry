"""Rate-limit backoff schedule tests (docs/11 § Retry policy).

429s get their own patient schedule, separate from other retryables:
exponential base 1s x2 with FULL jitter, capped, Retry-After honoured,
cancellation observed DURING the sleep, and the cost-budget pre-check run
per attempt. Env knobs: FOUNDRY_RATE_LIMIT_MAX_ATTEMPTS (default 8),
FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S (default 60).
"""

from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from typing import Any

import httpx
import pytest

from foundry.core import (
    CostBudget,
    CredentialsRef,
    FoundryMessage,
    MessageRole,
    ResolvedCredentials,
    Session,
    TextBlock,
)
from foundry.core.errors import ProviderRateLimitError, RunCancelled
from foundry.providers import (
    ModelBinding,
    RetryInfo,
    parse_retry_after,
    rate_limit_max_attempts,
    rate_limit_max_backoff_s,
    resolve,
)
from foundry.providers._base import ProviderAdapter


class FakeSecrets:
    def resolve(self, ref: CredentialsRef | None) -> ResolvedCredentials:
        return ResolvedCredentials(kind="env", secret="fake-key-for-tests")


class RecordingLogger:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def info(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def named(self, name: str) -> list[dict[str, Any]]:
        return [payload for event, payload in self.events if event == name]


def _messages() -> list[FoundryMessage]:
    return [
        FoundryMessage(role=MessageRole.USER, content=[TextBlock(text="hi")])
    ]


def _ok() -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": '{"greeting": "hi"}'}],
        "stop_reason": "end_turn",
        "model": "claude-haiku-4-5",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _adapter(handler: Any) -> ProviderAdapter:
    return resolve(
        ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        FakeSecrets(),
        transport=httpx.MockTransport(handler),
    )


# --- env knobs -----------------------------------------------------------------


@pytest.mark.unit
def test_env_knob_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_RATE_LIMIT_MAX_ATTEMPTS", raising=False)
    monkeypatch.delenv("FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S", raising=False)
    assert rate_limit_max_attempts() == 8
    assert rate_limit_max_backoff_s() == 60.0


@pytest.mark.unit
def test_env_knob_overrides_and_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S", "12.5")
    assert rate_limit_max_attempts() == 3
    assert rate_limit_max_backoff_s() == 12.5
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_MAX_ATTEMPTS", "not-a-number")
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S", "-4")
    assert rate_limit_max_attempts() == 8
    assert rate_limit_max_backoff_s() == 60.0


# --- schedule shape --------------------------------------------------------------


@pytest.mark.unit
def test_full_jitter_is_bounded_by_exponential_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S", raising=False)
    adapter = _adapter(lambda request: httpx.Response(200, json=_ok()))
    for attempt, ceiling in ((1, 1.0), (2, 2.0), (3, 4.0), (6, 32.0)):
        for _ in range(50):
            delay = adapter._rate_limit_delay(attempt, None)
            assert 0.0 <= delay <= ceiling, (attempt, delay)


@pytest.mark.unit
def test_backoff_cap_applies_to_late_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S", "5")
    adapter = _adapter(lambda request: httpx.Response(200, json=_ok()))
    for _ in range(50):
        assert adapter._rate_limit_delay(10, None) <= 5.0
    # Retry-After beyond the cap is capped too.
    assert adapter._rate_limit_delay(1, 120.0) == 5.0


@pytest.mark.unit
def test_retry_after_wins_over_jitter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S", raising=False)
    adapter = _adapter(lambda request: httpx.Response(200, json=_ok()))
    assert adapter._rate_limit_delay(1, 0.589) == 0.589
    assert adapter._rate_limit_delay(5, 2.0) == 2.0


# --- Retry-After parsing ----------------------------------------------------------


@pytest.mark.unit
def test_parse_retry_after_header_and_message_hints() -> None:
    assert parse_retry_after("2", "") == 2.0
    assert parse_retry_after("1.5", "") == 1.5
    assert parse_retry_after(None, "Please try again in 589ms.") == 0.589
    assert parse_retry_after(None, "please try again in 2 seconds") == 2.0
    assert parse_retry_after(None, "Try again in 1.25s") == 1.25
    # header wins over the message hint
    assert parse_retry_after("3", "Please try again in 589ms.") == 3.0
    # HTTP-date form / garbage headers fall back to the message, then None
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT", "nope") is None
    assert parse_retry_after(None, "rate limited, no hint") is None


@pytest.mark.unit
async def test_classified_429_carries_retry_after_in_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "7"},
            json={"error": {"message": "slow down"}},
        )

    adapter = _adapter(handler)
    with pytest.raises(ProviderRateLimitError) as excinfo:
        await adapter._attempt(_messages(), [], adapter._settings)
    assert excinfo.value.context["retry_after_s"] == 7.0


@pytest.mark.unit
async def test_openai_style_body_hint_lands_in_context() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            json={"error": {"message": "Please try again in 589ms."}},
        )

    adapter = _adapter(handler)
    with pytest.raises(ProviderRateLimitError) as excinfo:
        await adapter._attempt(_messages(), [], adapter._settings)
    assert excinfo.value.context["retry_after_s"] == pytest.approx(0.589)


# --- the retry loop ---------------------------------------------------------------


@pytest.mark.unit
async def test_retry_after_is_honoured_and_reported() -> None:
    """The computed delay follows the provider's Retry-After hint and is
    surfaced via provider.retry (log) + the on_retry callback."""
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0.02"},
                json={"error": {"message": "slow down"}},
            )
        return httpx.Response(200, json=_ok())

    adapter = _adapter(handler)
    logger = RecordingLogger()
    session = Session.new(project="t", logger=logger)
    retries: list[RetryInfo] = []
    response = await adapter.generate(
        _messages(), [], session=session, on_retry=retries.append
    )
    assert response.usage.input_tokens == 10
    assert attempts == 2
    assert len(retries) == 1
    assert retries[0].rate_limited is True
    assert retries[0].retry_after_s == pytest.approx(0.02)
    assert retries[0].delay_s == pytest.approx(0.02)
    logged = logger.named("provider.retry")
    assert len(logged) == 1
    assert logged[0]["rate_limited"] is True
    assert logged[0]["delay_s"] == pytest.approx(0.02)


@pytest.mark.unit
async def test_rate_limit_max_attempts_env_bounds_the_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_MAX_ATTEMPTS", "3")
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S", "0.01")
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    adapter = _adapter(handler)
    with pytest.raises(ProviderRateLimitError):
        await adapter.generate(_messages(), [])
    assert attempts == 3


@pytest.mark.unit
async def test_cancellation_is_observed_mid_backoff() -> None:
    """A long Retry-After sleep must not pin a cancelled run: the token is
    checked DURING the wait (bounded slices), not after it."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "30"},
            json={"error": {"message": "slow down"}},
        )

    adapter = _adapter(handler)
    session = Session.new(project="t")

    async def cancel_soon() -> None:
        await asyncio.sleep(0.05)
        session.cancel_token.cancel("operator hit cancel")

    started = time.monotonic()
    task = asyncio.create_task(cancel_soon())
    with pytest.raises(RunCancelled) as excinfo:
        await adapter.generate(_messages(), [], session=session)
    await task
    elapsed = time.monotonic() - started
    assert elapsed < 5.0, f"cancel took {elapsed:.1f}s — slept through backoff"
    assert excinfo.value.context["reason"] == "operator hit cancel"


@pytest.mark.unit
async def test_budget_pre_check_runs_per_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S", "0.01")
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(
                429, json={"error": {"message": "slow down"}}
            )
        return httpx.Response(200, json=_ok())

    adapter = _adapter(handler)
    logger = RecordingLogger()
    session = Session.new(
        project="t",
        logger=logger,
        cost_budget=CostBudget(max_usd=Decimal("1.00")),
    )
    await adapter.generate(_messages(), [], session=session)
    checks = logger.named("provider.budget_check")
    assert len(checks) == attempts == 3, (
        "the pre-call budget check must run before EVERY attempt"
    )
