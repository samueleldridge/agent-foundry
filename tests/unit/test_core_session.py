"""Session / RunId / CancelToken / CostBudget tests (docs/10 § Session)."""

from __future__ import annotations

import asyncio
from decimal import Decimal

import pytest
from pydantic import ValidationError

from foundry.core import CancelToken, CostBudget, RunId, Session
from foundry.core.errors import CostBudgetExceeded


@pytest.mark.unit
def test_run_id_new_is_valid_and_sortable() -> None:
    ids = [RunId.new() for _ in range(1000)]
    assert ids == sorted(ids)
    for rid in ids[:10]:
        assert RunId.validate(rid) == rid
        assert len(rid) == 26


@pytest.mark.unit
def test_run_id_validate_rejects_bad_values() -> None:
    with pytest.raises(ValueError):
        RunId.validate("too-short")
    with pytest.raises(ValueError):
        RunId.validate("!" * 26)


@pytest.mark.unit
def test_session_is_frozen() -> None:
    session = Session.new(project="hello")
    with pytest.raises(ValidationError):
        session.run_id = RunId.new()  # type: ignore[misc]


@pytest.mark.unit
async def test_cancel_token_wait_and_reason() -> None:
    token = CancelToken()
    assert not token.cancelled()
    assert token.reason is None

    async def waiter() -> str:
        await token.wait_cancelled()
        return "resolved"

    task = asyncio.create_task(waiter())
    await asyncio.sleep(0)
    token.cancel("user_abort")
    assert await asyncio.wait_for(task, timeout=1) == "resolved"
    assert token.cancelled()
    assert token.reason == "user_abort"
    # second cancel does not overwrite the reason
    token.cancel("timeout")
    assert token.reason == "user_abort"


@pytest.mark.unit
def test_cost_budget_check_raises_pre_call_with_context() -> None:
    budget = CostBudget(max_usd=Decimal("0.01"))
    budget.check(Decimal("0.005"))  # fine
    with pytest.raises(CostBudgetExceeded) as excinfo:
        budget.check(Decimal("0.02"))
    ctx = excinfo.value.context
    assert ctx["max_usd"] == "0.01"
    assert ctx["accumulated_usd"] == "0"
    assert ctx["estimated_usd"] == "0.02"


@pytest.mark.unit
def test_cost_budget_record_accumulates() -> None:
    budget = CostBudget(max_usd=Decimal("0.01"))
    budget.record(Decimal("0.008"))
    assert budget.remaining_usd() == Decimal("0.002")
    with pytest.raises(CostBudgetExceeded):
        budget.check(Decimal("0.003"))
    budget.check(Decimal("0.002"))  # exactly at budget is allowed
