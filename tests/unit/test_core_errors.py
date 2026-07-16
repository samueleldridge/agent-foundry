"""FoundryError hierarchy contract tests (docs/10 § Exception hierarchy)."""

from __future__ import annotations

import json

import pytest

from foundry.core import errors
from foundry.core.errors import CostBudgetExceeded, FoundryError, ProviderRateLimitError


def _all_foundry_error_classes() -> list[type[FoundryError]]:
    out: list[type[FoundryError]] = []
    stack: list[type[FoundryError]] = [FoundryError]
    while stack:
        cls = stack.pop()
        out.append(cls)
        stack.extend(cls.__subclasses__())
    return out


@pytest.mark.unit
@pytest.mark.parametrize(
    "cls", _all_foundry_error_classes(), ids=lambda c: c.__name__
)
def test_to_dict_is_json_serialisable(cls: type[FoundryError]) -> None:
    if cls is errors.ApprovalRequired:
        # Typed control-flow signature (Phase 7, docs/32) — covered by
        # test_approval_required_shape below.
        pytest.skip("ApprovalRequired has a dedicated constructor test")
    if cls is errors.ProjectUnavailableError:
        # Typed unavailability signature (docs/72 § Failure modes) —
        # covered by test_project_unavailable_shape below.
        pytest.skip("ProjectUnavailableError has a dedicated constructor test")
    cause = ValueError("inner cause")
    exc = cls("boom", context={"key": "value", "n": 3}, cause=cause)
    d = exc.to_dict()
    json.dumps(d)  # must not raise
    assert d["error_class"] == cls.__name__
    assert d["message"] == "boom"
    assert d["context"] == {"key": "value", "n": 3}
    assert d["cause_chain"] == [{"error_class": "ValueError", "message": "inner cause"}]


@pytest.mark.unit
def test_project_unavailable_shape() -> None:
    """ProjectUnavailableError is typed unavailability (docs/72 § Failure
    modes): project + missing env vars + operator remedy, all in context."""
    cause = ValueError("inner cause")
    exc = errors.ProjectUnavailableError(
        "project 'rag_hello' is unavailable",
        project="rag_hello",
        env_vars=["COHERE_API_KEY"],
        remedy="set COHERE_API_KEY and restart foundry studio",
        cause=cause,
    )
    assert exc.project == "rag_hello"
    assert exc.env_vars == ["COHERE_API_KEY"]
    assert "COHERE_API_KEY" in exc.remedy
    d = exc.to_dict()
    json.dumps(d)  # must not raise
    assert d["error_class"] == "ProjectUnavailableError"
    assert d["context"]["project"] == "rag_hello"
    assert d["context"]["env_vars"] == ["COHERE_API_KEY"]
    assert d["cause_chain"] == [
        {"error_class": "ValueError", "message": "inner cause"}
    ]


@pytest.mark.unit
def test_approval_required_shape() -> None:
    """ApprovalRequired is typed control flow (Phase 7, docs/32): stable
    approval_id + operator-facing prompt + audit context."""
    exc = errors.ApprovalRequired(
        approval_id="send-email-abc123",
        prompt="Send email to external counterparty?",
        context={"recipient": "x@example.com"},
        timeout_s=900,
        on_timeout="reject",
    )
    assert exc.approval_id == "send-email-abc123"
    assert exc.prompt == "Send email to external counterparty?"
    assert exc.approval_context == {"recipient": "x@example.com"}
    d = exc.to_dict()
    json.dumps(d)
    assert d["context"]["approval_id"] == "send-email-abc123"
    assert "approval required" in d["message"]


@pytest.mark.unit
def test_cause_chain_walks_nested_causes() -> None:
    inner = KeyError("innermost")
    mid = FoundryError("middle", cause=inner)
    outer = FoundryError("outer", cause=mid)
    chain = outer.to_dict()["cause_chain"]
    assert [c["error_class"] for c in chain] == ["FoundryError", "KeyError"]


@pytest.mark.unit
def test_every_public_error_subclasses_foundry_error() -> None:
    for name in errors.__all__:
        cls = getattr(errors, name)
        assert issubclass(cls, FoundryError), name


@pytest.mark.unit
def test_retryable_classification() -> None:
    assert ProviderRateLimitError("x").retryable is True
    assert errors.ProviderAuthError("x").retryable is False
    assert errors.ProviderTimeoutError("x").retryable is True


@pytest.mark.unit
def test_cost_budget_exceeded_is_orchestration_error() -> None:
    assert issubclass(CostBudgetExceeded, errors.OrchestrationError)
