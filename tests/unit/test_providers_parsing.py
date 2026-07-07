"""Provider response parsing + error classification + cost tests."""

from __future__ import annotations

from decimal import Decimal

import pytest

from foundry.core import CredentialsRef, ResolvedCredentials, StopReason, TokenUsage
from foundry.core.errors import (
    ProviderAuthError,
    ProviderContentPolicyError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUnexpectedError,
)
from foundry.providers import (
    ModelBinding,
    ProviderAdapter,
    estimate_cost,
    load_capabilities,
    resolve,
)


class FakeSecrets:
    def resolve(self, ref: CredentialsRef | None) -> ResolvedCredentials:
        return ResolvedCredentials(kind="env", secret="fake-key-for-tests")


def _adapter(provider: str, model: str) -> ProviderAdapter:
    return resolve(ModelBinding(provider=provider, model=model), FakeSecrets())


# --- Anthropic ---------------------------------------------------------------


@pytest.mark.unit
def test_anthropic_parse_response_populates_usage_and_stop_reason() -> None:
    adapter = _adapter("anthropic", "claude-haiku-4-5")
    payload = {
        "id": "msg_01",
        "model": "claude-haiku-4-5",
        "content": [{"type": "text", "text": "Hello, world!"}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": 20,
            "output_tokens": 10,
            "cache_read_input_tokens": 5,
            "cache_creation_input_tokens": 3,
        },
    }
    response = adapter._parse_response(payload, latency_ms=42)
    assert response.provider == "anthropic"
    assert response.stop_reason is StopReason.END_TURN
    assert response.usage.input_tokens == 20
    assert response.usage.output_tokens == 10
    assert response.usage.cached_read_tokens == 5
    assert response.usage.cached_write_tokens == 3
    assert response.usage.reasoning_tokens == 0  # non-reasoning: zero, not None
    assert response.message.content[0].text == "Hello, world!"  # type: ignore[union-attr]


@pytest.mark.unit
def test_anthropic_stop_reason_mapping() -> None:
    adapter = _adapter("anthropic", "claude-haiku-4-5")
    for raw, expected in [
        ("max_tokens", StopReason.MAX_TOKENS),
        ("stop_sequence", StopReason.STOP_SEQUENCE),
        ("tool_use", StopReason.TOOL_USE),
    ]:
        payload = {"content": [], "stop_reason": raw, "usage": {}}
        assert adapter._parse_response(payload, 1).stop_reason is expected


# --- OpenAI -------------------------------------------------------------------


def _openai_payload(
    reasoning_tokens: int | None,
    finish_reason: str = "stop",
    completion_tokens: int = 8,
) -> dict[str, object]:
    usage: dict[str, object] = {
        "prompt_tokens": 15,
        "completion_tokens": completion_tokens,
    }
    if reasoning_tokens is not None:
        usage["completion_tokens_details"] = {"reasoning_tokens": reasoning_tokens}
    return {
        "id": "chatcmpl-01",
        "model": "o3-mini",
        "choices": [
            {
                "message": {"role": "assistant", "content": '{"greeting": "hi"}'},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }


@pytest.mark.unit
def test_openai_reasoning_tokens_populated_for_reasoning_model() -> None:
    adapter = _adapter("openai", "o3-mini")
    # completion_tokens INCLUDES reasoning per the OpenAI API; the adapter
    # must split them so downstream cost math doesn't double-bill.
    response = adapter._parse_response(
        _openai_payload(reasoning_tokens=128, completion_tokens=150), 10
    )
    assert response.usage.reasoning_tokens == 128
    assert response.usage.input_tokens == 15
    assert response.usage.output_tokens == 22  # 150 - 128, not 150


@pytest.mark.unit
def test_openai_reasoning_tokens_not_double_billed() -> None:
    """Regression (Phase 1 review): actual-cost estimate for an o-series call
    must bill completion_tokens once at the output rate."""
    adapter = _adapter("openai", "o3-mini")
    caps = load_capabilities("openai", "o3-mini")
    response = adapter._parse_response(
        _openai_payload(reasoning_tokens=900, completion_tokens=1000), 10
    )
    billed = estimate_cost(caps, response.usage)
    expected = (
        Decimal(15) * caps.pricing.input_per_1m
        + Decimal(1000) * caps.pricing.output_per_1m
    ) / Decimal(1_000_000)
    assert billed == expected


@pytest.mark.unit
def test_openai_reasoning_tokens_zero_when_absent() -> None:
    adapter = _adapter("openai", "gpt-4o")
    response = adapter._parse_response(_openai_payload(reasoning_tokens=None), 10)
    assert response.usage.reasoning_tokens == 0


@pytest.mark.unit
def test_openai_reasoning_model_uses_max_completion_tokens() -> None:
    from foundry.providers import ModelSettings

    reasoning = _adapter("openai", "o3-mini")
    plain = _adapter("openai", "gpt-4o")
    settings = ModelSettings(max_tokens=100)
    r_body = reasoning._build_request([], [], settings).body
    p_body = plain._build_request([], [], settings).body
    assert r_body["max_completion_tokens"] == 100
    assert "max_tokens" not in r_body
    assert p_body["max_tokens"] == 100
    assert "max_completion_tokens" not in p_body


@pytest.mark.unit
def test_openai_content_filter_classified_as_content_policy() -> None:
    adapter = _adapter("openai", "gpt-4o")
    err = adapter._classify_http_error(
        400, {"error": {"code": "content_filter", "message": "refused"}}
    )
    assert isinstance(err, ProviderContentPolicyError)


# --- Shared error classification ----------------------------------------------


@pytest.mark.unit
@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (401, ProviderAuthError),
        (403, ProviderAuthError),
        (429, ProviderRateLimitError),
        (408, ProviderTimeoutError),
        (500, ProviderUnexpectedError),
    ],
)
def test_http_status_classification(status: int, expected: type[Exception]) -> None:
    adapter = _adapter("anthropic", "claude-haiku-4-5")
    err = adapter._classify_http_error(status, {"error": {"message": "x"}})
    assert isinstance(err, expected)
    assert err.context["http_status"] == status


# --- Cost estimation -----------------------------------------------------------


@pytest.mark.unit
def test_estimate_cost_matches_hand_computed_value() -> None:
    caps = load_capabilities("anthropic", "claude-sonnet-4-5")
    usage = TokenUsage(
        input_tokens=1000,
        output_tokens=500,
        cached_read_tokens=2000,
        cached_write_tokens=100,
    )
    # 1000*3/1M + 500*15/1M + 2000*0.3/1M + 100*3.75/1M
    expected = (
        Decimal("0.003")
        + Decimal("0.0075")
        + Decimal("0.0006")
        + Decimal("0.000375")
    )
    assert estimate_cost(caps, usage) == expected


@pytest.mark.unit
def test_estimate_cost_counts_reasoning_tokens_as_output() -> None:
    caps = load_capabilities("openai", "o3-mini")
    usage = TokenUsage(input_tokens=0, output_tokens=100, reasoning_tokens=900)
    # (100 + 900) * 4.4 / 1M
    assert estimate_cost(caps, usage) == Decimal("1000") * Decimal("4.4") / Decimal(
        1_000_000
    )
