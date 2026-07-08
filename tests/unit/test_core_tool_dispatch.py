"""ToolRegistry.dispatch behaviour (docs/20 § Dispatch + § Error semantics)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, Field

from foundry.core import (
    RegisteredTool,
    RetryPolicy,
    Session,
    ToolDescriptor,
    ToolRegistry,
    validate_handler_signature,
)
from foundry.core.errors import (
    ConnectionAuthError,
    ConnectionTimeoutError,
    ToolHandlerError,
    ToolInputValidationError,
    ToolNotAllowedError,
    ToolNotFoundError,
    ToolOutputValidationError,
)
from foundry.core.events import ToolCompleted, ToolStarted
from foundry.core.tool import RunContext


class EchoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1)


class EchoOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    echoed: str


def _ctx(*, timeout_s: float | None = None, connections: Any = None) -> RunContext:
    return RunContext(
        run_id="R" * 26,
        agent_name="tester",
        session=Session.new(project="unit"),
        tool_ref="local/echo@v1",
        timeout_s=timeout_s,
        retry_policy=RetryPolicy(initial_delay_s=0.01, max_delay_s=0.02),
        connections=connections,
    )


def _tool(handler: Any, *, auth_error_retry: bool = False) -> RegisteredTool:
    return RegisteredTool(
        descriptor=ToolDescriptor(
            name="echo", ref="local/echo", version="v1", description="echoes"
        ),
        input_schema=EchoIn,
        output_schema=EchoOut,
        handler=handler,
        timeout_s=5.0,
        auth_error_retry=auth_error_retry,
    )


def _registry(handler: Any, **kwargs: Any) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(_tool(handler, **kwargs))
    return registry


async def _ok_handler(inputs: BaseModel, ctx: RunContext) -> EchoOut:
    assert isinstance(inputs, EchoIn)
    return EchoOut(echoed=inputs.text)


class _Emitted:
    def __init__(self) -> None:
        self.events: list[tuple[type, dict[str, Any]]] = []

    def __call__(self, event_cls: type, **fields: Any) -> None:
        self.events.append((event_cls, fields))


@pytest.mark.unit
async def test_dispatch_happy_path_validates_and_emits() -> None:
    registry = _registry(_ok_handler)
    emitted = _Emitted()
    out = await registry.dispatch("echo", ["echo"], {"text": "hi"}, _ctx(), emitted)
    assert isinstance(out, EchoOut) and out.echoed == "hi"
    kinds = [cls for cls, _ in emitted.events]
    assert kinds == [ToolStarted, ToolCompleted]
    completed = emitted.events[1][1]
    assert completed["success"] is True
    assert completed["tool_ref"] == "local/echo"
    assert completed["tool_version"] == "v1"


@pytest.mark.unit
async def test_allowlist_refusal_names_agent_tool_and_allowlist() -> None:
    registry = _registry(_ok_handler)
    with pytest.raises(ToolNotAllowedError) as excinfo:
        await registry.dispatch("echo", ["other_tool"], {"text": "x"}, _ctx())
    assert excinfo.value.context == {
        "agent": "tester",
        "tool": "echo",
        "allowlist": ["other_tool"],
    }


@pytest.mark.unit
async def test_unknown_tool_raises_not_found() -> None:
    registry = _registry(_ok_handler)
    with pytest.raises(ToolNotFoundError):
        await registry.dispatch("nope", ["nope"], {}, _ctx())


@pytest.mark.unit
async def test_invalid_input_rejected_before_handler_runs() -> None:
    called = False

    async def handler(inputs: BaseModel, ctx: RunContext) -> EchoOut:
        nonlocal called
        called = True
        return EchoOut(echoed="x")

    registry = _registry(handler)
    with pytest.raises(ToolInputValidationError) as excinfo:
        await registry.dispatch("echo", ["echo"], {"text": ""}, _ctx())
    assert not called
    assert excinfo.value.context["input_schema"] == "EchoIn"


@pytest.mark.unit
async def test_wrong_output_shape_raises_output_validation_error() -> None:
    async def handler(inputs: BaseModel, ctx: RunContext) -> Any:
        return {"unexpected": True}

    registry = _registry(handler)
    emitted = _Emitted()
    with pytest.raises(ToolOutputValidationError):
        await registry.dispatch("echo", ["echo"], {"text": "x"}, _ctx(), emitted)
    completed = emitted.events[-1][1]
    assert completed["success"] is False
    assert completed["error_category"] == "ToolOutputValidationError"


@pytest.mark.unit
async def test_dict_output_matching_schema_is_validated_in() -> None:
    async def handler(inputs: BaseModel, ctx: RunContext) -> Any:
        return {"echoed": "from-a-dict"}

    registry = _registry(handler)
    out = await registry.dispatch("echo", ["echo"], {"text": "x"}, _ctx())
    assert isinstance(out, EchoOut)


@pytest.mark.unit
async def test_arbitrary_exception_wrapped_as_tool_handler_error() -> None:
    async def handler(inputs: BaseModel, ctx: RunContext) -> EchoOut:
        raise KeyError("boom")

    registry = _registry(handler)
    with pytest.raises(ToolHandlerError) as excinfo:
        await registry.dispatch("echo", ["echo"], {"text": "x"}, _ctx())
    assert excinfo.value.context["cause_type"] == "KeyError"


@pytest.mark.unit
async def test_timeout_becomes_tool_handler_error() -> None:
    async def handler(inputs: BaseModel, ctx: RunContext) -> EchoOut:
        await asyncio.sleep(5)
        return EchoOut(echoed="never")

    registry = _registry(handler)
    with pytest.raises(ToolHandlerError) as excinfo:
        await registry.dispatch(
            "echo", ["echo"], {"text": "x"}, _ctx(timeout_s=0.05)
        )
    assert excinfo.value.context["cause_type"] == "TimeoutError"


@pytest.mark.unit
async def test_retry_loop_retries_configured_errors_then_succeeds() -> None:
    attempts = 0

    async def handler(inputs: BaseModel, ctx: RunContext) -> EchoOut:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise ConnectionTimeoutError("flaky")
        return EchoOut(echoed="ok")

    registry = _registry(handler)
    ctx = RunContext(
        run_id="R" * 26,
        agent_name="tester",
        session=Session.new(project="unit"),
        tool_ref="local/echo@v1",
        retry_policy=RetryPolicy(
            max_attempts=3,
            initial_delay_s=0.01,
            max_delay_s=0.02,
            retryable_errors=["ConnectionTimeoutError"],
        ),
    )
    emitted = _Emitted()
    out = await registry.dispatch("echo", ["echo"], {"text": "x"}, ctx, emitted)
    assert out.echoed == "ok"
    assert attempts == 3
    assert emitted.events[-1][1]["retry_count"] == 2


@pytest.mark.unit
async def test_failure_after_retries_reports_real_retry_count() -> None:
    """Regression (Phase 2a review): when _run_with_retries exhausts its
    attempts and raises, the failure-path tool.completed event must report
    the retries that actually ran — not 0."""

    async def handler(inputs: BaseModel, ctx: RunContext) -> EchoOut:
        raise ConnectionTimeoutError("always down")

    registry = _registry(handler)
    ctx = RunContext(
        run_id="R" * 26,
        agent_name="tester",
        session=Session.new(project="unit"),
        tool_ref="local/echo@v1",
        retry_policy=RetryPolicy(
            max_attempts=3,
            initial_delay_s=0.01,
            max_delay_s=0.02,
            retryable_errors=["ConnectionTimeoutError"],
        ),
    )
    emitted = _Emitted()
    with pytest.raises(ConnectionTimeoutError):
        await registry.dispatch("echo", ["echo"], {"text": "x"}, ctx, emitted)
    completed = emitted.events[-1][1]
    assert completed["success"] is False
    assert completed["error_category"] == "ConnectionTimeoutError"
    assert completed["retry_count"] == 2  # 3 attempts = 2 retries


class _FakeAccessor:
    """Minimal ConnectionAccessor double for the on_auth_error path."""

    def __init__(self, evicts: bool) -> None:
        self._evicts = evicts
        self.on_auth_error_calls = 0

    async def get(self, slot: str) -> Any:
        raise AssertionError("not used")

    async def health(self, slot: str) -> Any:
        raise AssertionError("not used")

    def descriptor(self, slot: str) -> Any:
        raise AssertionError("not used")

    async def on_auth_error(self) -> bool:
        self.on_auth_error_calls += 1
        return self._evicts

    async def release_all(self) -> None:
        pass


@pytest.mark.unit
async def test_auth_error_evicts_and_retries_once() -> None:
    attempts = 0

    async def handler(inputs: BaseModel, ctx: RunContext) -> EchoOut:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ConnectionAuthError("401 from service")
        return EchoOut(echoed="recovered")

    accessor = _FakeAccessor(evicts=True)
    registry = _registry(handler, auth_error_retry=True)
    out = await registry.dispatch(
        "echo", ["echo"], {"text": "x"}, _ctx(connections=accessor)
    )
    assert out.echoed == "recovered"
    assert attempts == 2
    assert accessor.on_auth_error_calls == 1


@pytest.mark.unit
async def test_second_auth_error_propagates() -> None:
    async def handler(inputs: BaseModel, ctx: RunContext) -> EchoOut:
        raise ConnectionAuthError("still 401")

    accessor = _FakeAccessor(evicts=True)
    registry = _registry(handler, auth_error_retry=True)
    with pytest.raises(ConnectionAuthError):
        await registry.dispatch(
            "echo", ["echo"], {"text": "x"}, _ctx(connections=accessor)
        )
    assert accessor.on_auth_error_calls == 1  # retried exactly once


@pytest.mark.unit
async def test_auth_error_without_on_auth_error_refresh_propagates() -> None:
    async def handler(inputs: BaseModel, ctx: RunContext) -> EchoOut:
        raise ConnectionAuthError("401")

    accessor = _FakeAccessor(evicts=True)
    registry = _registry(handler, auth_error_retry=False)
    with pytest.raises(ConnectionAuthError):
        await registry.dispatch(
            "echo", ["echo"], {"text": "x"}, _ctx(connections=accessor)
        )
    assert accessor.on_auth_error_calls == 0


@pytest.mark.unit
def test_handler_signature_enforced() -> None:
    def sync_handler(inputs: Any, ctx: Any) -> None: ...

    async def wrong_names(data: Any, context: Any) -> None: ...

    async def ok(inputs: Any, ctx: Any) -> None: ...

    with pytest.raises(ToolHandlerError):
        validate_handler_signature(sync_handler, where="x.py")
    with pytest.raises(ToolHandlerError) as excinfo:
        validate_handler_signature(wrong_names, where="x.py")
    assert "data, context" in str(excinfo.value)
    assert validate_handler_signature(ok, where="x.py") is ok


@pytest.mark.unit
async def test_input_preview_redacts_secret_looking_keys() -> None:
    class SecretIn(BaseModel):
        text: str
        api_key: str

    tool = RegisteredTool(
        descriptor=ToolDescriptor(name="s", ref="local/s", version="v1"),
        input_schema=SecretIn,
        output_schema=EchoOut,
        handler=_ok_handler,
    )
    registry = ToolRegistry()
    registry.register(tool)
    emitted = _Emitted()

    async def handler(inputs: BaseModel, ctx: RunContext) -> EchoOut:
        return EchoOut(echoed="x")

    registry.register(
        RegisteredTool(
            descriptor=tool.descriptor,
            input_schema=SecretIn,
            output_schema=EchoOut,
            handler=handler,
        )
    )
    await registry.dispatch(
        "s", ["s"], {"text": "hi", "api_key": "sk-super-secret-value"},
        _ctx(), emitted,
    )
    preview = emitted.events[0][1]["input_preview"]
    assert "sk-super-secret-value" not in preview
    assert "<redacted>" in preview
