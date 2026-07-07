"""Session: run-scoped bundle of run_id, logger, tracer, checkpointer, budget.

``Session`` itself is immutable; the mutable bits inside it (``CancelToken``,
``CostBudget``) carry their own state. See docs/10 § Session.

Phase 1 ships a minimal CancelToken (asyncio.Event under the hood) and a
no-op logger/tracer placeholder. Full OTel + structlog wiring lands in
Phase 9; the surface here will not change.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from foundry.core.errors import CostBudgetExceeded
from foundry.core.types import RunId

# --- CancelToken ------------------------------------------------------------


class CancelToken:
    """Minimal cooperative-cancel signal. Tasks await ``wait_cancelled`` or
    poll ``cancelled()``; the runtime calls ``cancel(reason)`` once."""

    __slots__ = ("_event", "_reason")

    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._reason: str | None = None

    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait_cancelled(self) -> None:
        await self._event.wait()

    def cancel(self, reason: str) -> None:
        if not self._event.is_set():
            self._reason = reason
            self._event.set()

    @property
    def reason(self) -> str | None:
        return self._reason


# --- CheckpointerHandle (opaque) -------------------------------------------


@runtime_checkable
class CheckpointerHandle(Protocol):
    """Opaque handle to the LangGraph checkpointer.

    Core never touches LangGraph types; the runtime adapter constructs the
    concrete handle and binds it to the session.
    """

    async def put(self, key: str, value: bytes) -> None: ...
    async def get(self, key: str) -> bytes | None: ...
    async def list_keys(self, prefix: str) -> list[str]: ...


class NoOpCheckpointer:
    """In-memory dummy checkpointer; used when no checkpointing is configured."""

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}

    async def put(self, key: str, value: bytes) -> None:
        self._store[key] = value

    async def get(self, key: str) -> bytes | None:
        return self._store.get(key)

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(k for k in self._store if k.startswith(prefix))


# --- CostBudget -------------------------------------------------------------


class CostBudget(BaseModel):
    """Per-run dollar cap. Provider adapter checks pre-call and records
    post-call. ``check`` raises ``CostBudgetExceeded`` if the next call would
    breach the cap."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    max_usd: Decimal
    accumulated_usd: Decimal = Decimal("0")

    def remaining_usd(self) -> Decimal:
        return self.max_usd - self.accumulated_usd

    def check(self, estimated_usd: Decimal) -> None:
        if self.accumulated_usd + estimated_usd > self.max_usd:
            raise CostBudgetExceeded(
                f"call would push spend to ${self.accumulated_usd + estimated_usd}, "
                f"budget ${self.max_usd}",
                context={
                    "max_usd": str(self.max_usd),
                    "accumulated_usd": str(self.accumulated_usd),
                    "estimated_usd": str(estimated_usd),
                    "remaining_usd": str(self.remaining_usd()),
                },
            )

    def record(self, actual_usd: Decimal) -> None:
        self.accumulated_usd += actual_usd


# --- Session ---------------------------------------------------------------


class Session(BaseModel):
    """Immutable bundle of run-scoped resources.

    Mutation-looking operations (entering a span, binding a logger) either
    produce a new Session or use a context-managed lexical scope.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    run_id: RunId
    project: str
    system_version: str = ""
    pin_set_hash: str = ""
    started_at: datetime

    logger: Any = None
    """structlog BoundLogger or compatible. Phase 1 leaves untyped to avoid
    pulling structlog into core."""

    tracer: Any = None
    """OTel tracer. Phase 1 leaves untyped; ``span()`` is a no-op stub here."""

    cancel_token: CancelToken
    checkpointer: CheckpointerHandle
    cost_budget: CostBudget | None = None

    @classmethod
    def new(
        cls,
        *,
        project: str,
        run_id: RunId | None = None,
        cost_budget: CostBudget | None = None,
        checkpointer: CheckpointerHandle | None = None,
        logger: Any = None,
        system_version: str = "",
        pin_set_hash: str = "",
    ) -> Session:
        return cls(
            run_id=run_id or RunId.new(),
            project=project,
            system_version=system_version,
            pin_set_hash=pin_set_hash,
            started_at=datetime.now(UTC),
            logger=logger,
            cancel_token=CancelToken(),
            checkpointer=checkpointer or NoOpCheckpointer(),
            cost_budget=cost_budget,
        )

    @asynccontextmanager
    async def span(self, name: str, **attrs: Any) -> AsyncIterator[None]:
        """No-op OTel span placeholder. Real wiring lands in Phase 9."""
        _ = name, attrs
        yield None


__all__ = [
    "CancelToken",
    "CheckpointerHandle",
    "CostBudget",
    "NoOpCheckpointer",
    "Session",
]
