"""Cost-budget enforcement + retry-loop tests against a mock transport."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from foundry.core import (
    CostBudget,
    CredentialsRef,
    FoundryMessage,
    MessageRole,
    ResolvedCredentials,
    RetryPolicy,
    Session,
    TextBlock,
)
from foundry.core.errors import CostBudgetExceeded, ProviderRateLimitError
from foundry.providers import ModelBinding, resolve


class FakeSecrets:
    def resolve(self, ref: CredentialsRef | None) -> ResolvedCredentials:
        return ResolvedCredentials(kind="env", secret="fake-key-for-tests")


def _messages() -> list[FoundryMessage]:
    return [
        FoundryMessage(role=MessageRole.SYSTEM, content=[TextBlock(text="greet")]),
        FoundryMessage(role=MessageRole.USER, content=[TextBlock(text="hello")]),
    ]


def _anthropic_ok() -> dict[str, object]:
    return {
        "content": [{"type": "text", "text": '{"greeting": "hi"}'}],
        "stop_reason": "end_turn",
        "model": "claude-haiku-4-5",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


@pytest.mark.unit
async def test_over_budget_raises_pre_call_and_makes_no_http_call() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=_anthropic_ok())

    adapter = resolve(
        ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        FakeSecrets(),
        transport=httpx.MockTransport(handler),
    )
    session = Session.new(
        project="t", cost_budget=CostBudget(max_usd=Decimal("0.0001"))
    )
    with pytest.raises(CostBudgetExceeded) as excinfo:
        await adapter.generate(_messages(), [], session=session)
    assert calls == 0, "budget must be enforced BEFORE the provider call"
    assert "max_usd" in excinfo.value.context


@pytest.mark.unit
async def test_within_budget_records_actual_cost_post_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_anthropic_ok())

    adapter = resolve(
        ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        FakeSecrets(),
        transport=httpx.MockTransport(handler),
    )
    budget = CostBudget(max_usd=Decimal("1.00"))
    session = Session.new(project="t", cost_budget=budget)
    response = await adapter.generate(_messages(), [], session=session)
    # 10 input * $1/1M + 5 output * $5/1M
    expected = Decimal("10") / 1_000_000 + Decimal("25") / 1_000_000
    assert response.cost_estimate_usd == expected
    assert budget.accumulated_usd == expected


@pytest.mark.unit
async def test_no_budget_means_no_enforcement() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_anthropic_ok())

    adapter = resolve(
        ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        FakeSecrets(),
        transport=httpx.MockTransport(handler),
    )
    session = Session.new(project="t")  # cost_budget=None
    response = await adapter.generate(_messages(), [], session=session)
    assert response.stop_reason.value == "end_turn"


@pytest.mark.unit
async def test_retry_loop_retries_rate_limit_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S", "0.01")
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(429, json={"error": {"message": "slow down"}})
        return httpx.Response(200, json=_anthropic_ok())

    adapter = resolve(
        ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        FakeSecrets(),
        retry_policy=RetryPolicy(max_attempts=3, initial_delay_s=0.01, jitter=False),
        transport=httpx.MockTransport(handler),
    )
    response = await adapter.generate(_messages(), [])
    assert attempts == 3
    assert response.usage.input_tokens == 10


@pytest.mark.unit
async def test_retry_loop_gives_up_after_max_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """429s follow the RATE-LIMIT schedule (FOUNDRY_RATE_LIMIT_MAX_
    ATTEMPTS), not RetryPolicy.max_attempts."""
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_MAX_ATTEMPTS", "2")
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S", "0.01")
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"error": {"message": "slow down"}})

    adapter = resolve(
        ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        FakeSecrets(),
        retry_policy=RetryPolicy(max_attempts=2, initial_delay_s=0.01, jitter=False),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(ProviderRateLimitError):
        await adapter.generate(_messages(), [])
    assert attempts == 2


@pytest.mark.unit
async def test_api_key_sent_in_header_not_body() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_anthropic_ok())

    adapter = resolve(
        ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        FakeSecrets(),
        transport=httpx.MockTransport(handler),
    )
    await adapter.generate(_messages(), [])
    headers = seen["headers"]
    assert isinstance(headers, dict)
    assert headers["x-api-key"] == "fake-key-for-tests"
    assert "fake-key-for-tests" not in json.dumps(seen["body"])
