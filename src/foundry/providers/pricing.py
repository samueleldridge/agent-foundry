"""Per-model cost estimation (docs/11 § Cost estimation).

Pricing is indicative, not authoritative — used for foundry-internal
budgeting (cost budgets, dev-time monitoring), never billing.
"""

from __future__ import annotations

from decimal import Decimal

from foundry.core import FoundryMessage, TextBlock, TokenUsage
from foundry.providers._types import ModelSettings, ProviderCapabilities

_ONE_MILLION = Decimal(1_000_000)

# Crude chars-per-token heuristic for the pre-call input estimate. Generous
# (i.e. errs high) is fine: the pre-call check is an upper bound by design.
_CHARS_PER_TOKEN = 4


def estimate_cost(capabilities: ProviderCapabilities, usage: TokenUsage) -> Decimal:
    """Actual cost of a completed call, from its token usage."""
    p = capabilities.pricing
    return (
        Decimal(usage.input_tokens) * p.input_per_1m / _ONE_MILLION
        + Decimal(usage.output_tokens + usage.reasoning_tokens)
        * p.output_per_1m
        / _ONE_MILLION
        + Decimal(usage.cached_read_tokens) * p.cache_read_per_1m / _ONE_MILLION
        + Decimal(usage.cached_write_tokens) * p.cache_write_per_1m / _ONE_MILLION
    )


def estimate_input_tokens(messages: list[FoundryMessage]) -> int:
    """Chars/4 heuristic over all textual content. Upper-bound-ish."""
    chars = 0
    for m in messages:
        for block in m.content:
            if isinstance(block, TextBlock):
                chars += len(block.text)
            else:
                chars += 256  # flat allowance for non-text blocks
    return max(1, chars // _CHARS_PER_TOKEN)


def estimate_pre_call_cost(
    capabilities: ProviderCapabilities,
    messages: list[FoundryMessage],
    settings: ModelSettings,
) -> Decimal:
    """Generous upper bound for the cost-budget pre-call check:
    estimated input tokens x input price + max output tokens x output price."""
    p = capabilities.pricing
    input_est = estimate_input_tokens(messages)
    max_out = settings.max_tokens or capabilities.max_output_tokens
    return (
        Decimal(input_est) * p.input_per_1m / _ONE_MILLION
        + Decimal(max_out) * p.output_per_1m / _ONE_MILLION
    )


__all__ = ["estimate_cost", "estimate_input_tokens", "estimate_pre_call_cost"]
