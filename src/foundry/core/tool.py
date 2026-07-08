"""Tool protocol, registry, and the dispatch path (docs/20 § ToolRegistry).

``ToolRegistry.dispatch`` is the single entry point every tool call flows
through: allowlist check → resolution → input validation → result-cache
lookup (opt-in per tool, docs/24 § Layer 3) → handler with retry + timeout →
output validation → result-cache store, with structured ``tool.started`` /
``tool.completed`` events around every ACTUAL handler invocation (cache hits
short-circuit before ``tool.started`` so tool_calls.jsonl counts real runs;
the hit itself is audited via ``cache.tool.hit``).

Concrete tool handlers live outside ``src/foundry`` (catalog/ or
projects/<name>/tools/) and are loaded via ``foundry.catalog.loader``; this
module stays import-clean (stdlib + pydantic + core-internal only).
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from foundry.core.cache import CacheScope, ResultCache, scoped_input_hash
from foundry.core.connection import ConnectionAccessor
from foundry.core.errors import (
    CacheError,
    ConnectionAuthError,
    FoundryError,
    ProviderError,
    RunCancelled,
    ToolError,
    ToolHandlerError,
    ToolInputValidationError,
    ToolNotAllowedError,
    ToolNotFoundError,
    ToolOutputValidationError,
)
from foundry.core.errors import (
    ConnectionError as FoundryConnectionError,
)
from foundry.core.events import (
    ToolCacheHit,
    ToolCacheMiss,
    ToolCacheStore,
    ToolCompleted,
    ToolStarted,
    WarningEvent,
)
from foundry.core.retrieval import RetrieverAccessor
from foundry.core.session import Session


class BackoffStrategy(StrEnum):
    CONSTANT = "constant"
    LINEAR = "linear"
    EXPONENTIAL = "exponential"


class RetryPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=3, ge=1, le=20)
    backoff: BackoffStrategy = BackoffStrategy.EXPONENTIAL
    initial_delay_s: float = Field(default=1.0, gt=0)
    max_delay_s: float = Field(default=30.0, gt=0)
    retryable_errors: list[str] = Field(
        default_factory=lambda: ["ProviderRateLimitError", "ProviderTimeoutError"]
    )
    jitter: bool = True

    def delay_for(self, attempt: int) -> float:
        """Backoff delay before retry number ``attempt`` (1-based)."""
        if self.backoff is BackoffStrategy.CONSTANT:
            delay = self.initial_delay_s
        elif self.backoff is BackoffStrategy.LINEAR:
            delay = self.initial_delay_s * attempt
        else:
            delay = self.initial_delay_s * (2 ** (attempt - 1))
        return min(delay, self.max_delay_s)


@runtime_checkable
class Tool(Protocol):
    name: str
    version: str
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]

    async def handle(self, inputs: BaseModel, ctx: RunContext) -> BaseModel: ...


class RunContext(BaseModel):
    """Handle threaded into tool handlers (docs/10 § RunContext).

    Valid for the duration of one ``handle`` invocation only; handlers MUST
    NOT store it. Connections come exclusively from ``ctx.connections``.
    """

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    run_id: str
    agent_name: str
    session: Session
    tool_ref: str
    timeout_s: float | None = None
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    connections: ConnectionAccessor | None = None
    """Slot-name → Connection accessor. None only for tools that declare no
    connection slots (accessing a slot then raises at the accessor layer)."""
    retrievers: RetrieverAccessor | None = None
    """Slot-name → Retriever accessor (docs/25 § RetrieverBinding). None when
    the agent declares no retrievers."""


class BaseTool:
    """Convenience base for hand-written tools. Subclasses define
    ``input_schema`` / ``output_schema`` and implement ``handle``."""

    name: str = ""
    version: str = ""
    input_schema: type[BaseModel] = BaseModel
    output_schema: type[BaseModel] = BaseModel

    async def handle(self, inputs: BaseModel, ctx: RunContext) -> BaseModel:
        raise NotImplementedError


ToolHandler = Callable[[BaseModel, RunContext], Awaitable[BaseModel]]

_HANDLER_PARAMS = ("inputs", "ctx")


def validate_handler_signature(handler: object, *, where: str) -> ToolHandler:
    """Enforce ``async def handle(inputs, ctx)`` (docs/20 § Handler rules 1).

    Raises ToolHandlerError naming the offending file when the shape is off.
    """
    if not inspect.iscoroutinefunction(handler):
        raise ToolHandlerError(
            f"tool handler at {where} must be an async function "
            "(`async def handle(inputs, ctx)`)",
            context={"where": where},
        )
    params = list(inspect.signature(handler).parameters)
    if tuple(params[:2]) != _HANDLER_PARAMS or len(params) != 2:
        raise ToolHandlerError(
            f"tool handler at {where} has signature ({', '.join(params)}); "
            "expected exactly (inputs, ctx) — the registry introspects by name",
            context={"where": where, "received_params": params},
        )
    return handler


class ToolDescriptor(BaseModel):
    """Serialisable summary of a registered tool (listing + observability)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    """Logical name — the SystemSpec.tools key agents allowlist."""
    ref: str
    version: str
    description: str = ""
    tags: list[str] = Field(default_factory=list)
    connection_slots: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class RegisteredTool:
    """A fully-resolved tool: spec-derived settings + imported handler."""

    descriptor: ToolDescriptor
    input_schema: type[BaseModel]
    output_schema: type[BaseModel]
    handler: ToolHandler
    timeout_s: float = 30.0
    retry_policy: RetryPolicy = field(default_factory=RetryPolicy)
    auth_error_retry: bool = False
    """True when any bound connection's refresh mode is on_auth_error: a
    ConnectionAuthError evicts + rebuilds via the accessor and the handler is
    retried once (docs/23 § Refresh)."""
    cacheable: bool = False
    """Result caching opt-in, from ToolSpec.cacheable (docs/24 § Layer 3)."""
    cache_ttl_s: int | None = None
    cache_scope: CacheScope = "project"

    @property
    def name(self) -> str:
        return self.descriptor.name

    @property
    def version(self) -> str:
        return self.descriptor.version

    async def handle(self, inputs: BaseModel, ctx: RunContext) -> BaseModel:
        return await self.handler(inputs, ctx)


_SENSITIVE_INPUT_KEY = re.compile(
    r"(password|secret|token|api_key|apikey|credential)", re.IGNORECASE
)
_PREVIEW_MAX_CHARS = 200

EmitFn = Callable[..., None]
"""Signature: emit(event_cls, **fields). The runtime's sequence-stamping
emitter conforms; None disables event emission (unit-test convenience)."""


def _preview(data: dict[str, Any]) -> str:
    redacted = {
        k: ("<redacted>" if _SENSITIVE_INPUT_KEY.search(k) else v)
        for k, v in data.items()
    }
    text = json.dumps(redacted, default=str)
    return text[:_PREVIEW_MAX_CHARS]


def input_hash(data: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, default=str).encode()
    ).hexdigest()[:16]


@dataclass
class _RetryTracker:
    """Mutable retry counter shared between dispatch and the retry loop, so
    tool.completed reports the real retry_count on BOTH success and failure."""

    count: int = 0


class ToolRegistry:
    """Loaded once at compile time; keyed by logical name AND (ref, version).

    ``dispatch`` is the single entry point through which tool calls flow
    (docs/20 § Dispatch). It never lets an arbitrary exception escape: every
    failure is a FoundryError subclass and every path emits tool.completed.
    """

    def __init__(self) -> None:
        self._by_name: dict[str, RegisteredTool] = {}
        self._by_ref: dict[tuple[str, str], RegisteredTool] = {}

    def register(self, tool: RegisteredTool) -> None:
        self._by_name[tool.descriptor.name] = tool
        self._by_ref[(tool.descriptor.ref, tool.descriptor.version)] = tool

    def get(self, name: str) -> RegisteredTool | None:
        return self._by_name.get(name)

    def get_by_ref(self, ref: str, version: str) -> RegisteredTool | None:
        return self._by_ref.get((ref, version))

    def list_all(self) -> list[ToolDescriptor]:
        return [t.descriptor for t in self._by_name.values()]

    def list_by_tag(self, tag: str) -> list[ToolDescriptor]:
        return [d for d in self.list_all() if tag in d.tags]

    def __contains__(self, name: str) -> bool:
        return name in self._by_name

    async def dispatch(
        self,
        name: str,
        agent_allowlist: list[str],
        raw_input: dict[str, Any],
        ctx: RunContext,
        emit: EmitFn | None = None,
    ) -> BaseModel:
        """Allowlist → resolve → validate in → handler (retry + timeout) →
        validate out. Raises ToolError / ConnectionError subclasses."""
        if name not in agent_allowlist:
            raise ToolNotAllowedError(
                f"agent {ctx.agent_name!r} is not allowed to call tool {name!r}; "
                f"allowlist: {sorted(agent_allowlist)}",
                context={
                    "agent": ctx.agent_name,
                    "tool": name,
                    "allowlist": sorted(agent_allowlist),
                },
            )
        tool = self._by_name.get(name)
        if tool is None:
            raise ToolNotFoundError(
                f"tool {name!r} is not registered; known tools: "
                f"{sorted(self._by_name)}",
                context={"tool": name, "known_tools": sorted(self._by_name)},
            )

        try:
            inputs = tool.input_schema.model_validate(raw_input)
        except ValidationError as exc:
            raise ToolInputValidationError(
                f"input for tool {name!r} failed validation against "
                f"{tool.input_schema.__name__}: {exc.errors()[0]['msg']} "
                f"(at {'/'.join(str(p) for p in exc.errors()[0]['loc'])})",
                context={
                    "tool": name,
                    "input_schema": tool.input_schema.__name__,
                    "errors": [
                        {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                        for e in exc.errors()
                    ],
                },
                cause=exc,
            ) from exc

        hashed = input_hash(raw_input)

        # Tool-result cache (docs/24 § Layer 3): exact-match lookup BEFORE the
        # tool.started event, so tool_calls.jsonl keeps meaning "the handler
        # actually ran". Cache errors fail open (warning + run the handler).
        result_cache = ctx.session.cache.tool_result
        use_cache = tool.cacheable and tool.cache_ttl_s is not None
        if use_cache and result_cache is not None:
            cached = await self._cache_lookup(tool, result_cache, hashed, ctx, emit)
            if cached is not None:
                return cached

        if emit is not None:
            emit(
                ToolStarted,
                agent_name=ctx.agent_name,
                tool_ref=tool.descriptor.ref,
                tool_version=tool.descriptor.version,
                input_hash=hashed,
                input_preview=_preview(raw_input),
            )

        started = time.monotonic()
        retries = _RetryTracker()
        try:
            raw_output = await self._run_with_retries(tool, inputs, ctx, retries)
            output = self._validate_output(tool, raw_output)
        except FoundryError as exc:
            if emit is not None:
                emit(
                    ToolCompleted,
                    agent_name=ctx.agent_name,
                    tool_ref=tool.descriptor.ref,
                    tool_version=tool.descriptor.version,
                    success=False,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    retry_count=retries.count,
                    error_category=type(exc).__name__,
                )
            raise

        if emit is not None:
            emit(
                ToolCompleted,
                agent_name=ctx.agent_name,
                tool_ref=tool.descriptor.ref,
                tool_version=tool.descriptor.version,
                success=True,
                latency_ms=int((time.monotonic() - started) * 1000),
                retry_count=retries.count,
                output_preview=_preview(output.model_dump(mode="json")),
            )
        if use_cache and result_cache is not None:
            await self._cache_store(tool, result_cache, hashed, output, ctx, emit)
        return output

    async def _cache_lookup(
        self,
        tool: RegisteredTool,
        cache: ResultCache,
        hashed: str,
        ctx: RunContext,
        emit: EmitFn | None,
    ) -> BaseModel | None:
        key = scoped_input_hash(
            tool.cache_scope, ctx.session.project, ctx.agent_name, hashed
        )
        try:
            hit = await cache.lookup(
                tool.descriptor.ref, tool.descriptor.version, key
            )
        except CacheError as exc:
            if emit is not None:
                emit(
                    WarningEvent,
                    agent_name=ctx.agent_name,
                    category="cache.tool.error",
                    message=f"tool-result cache lookup failed for "
                    f"{tool.name!r}; running the handler (fail-open): {exc}",
                    error_class=type(exc).__name__,
                )
            return None
        if hit is None:
            if emit is not None:
                emit(
                    ToolCacheMiss,
                    agent_name=ctx.agent_name,
                    tool_ref=tool.descriptor.ref,
                    tool_version=tool.descriptor.version,
                )
            return None
        try:
            output = tool.output_schema.model_validate(hit.output)
        except ValidationError as exc:
            # Corrupted / schema-drifted entry: treat as a miss; the handler
            # runs and its store overwrites the bad entry (docs/24 § Failure
            # modes: CacheCorruptedEntry → eviction + miss + warning).
            if emit is not None:
                emit(
                    WarningEvent,
                    agent_name=ctx.agent_name,
                    category="cache.tool.corrupted_entry",
                    message=f"cached output for tool {tool.name!r} failed "
                    f"validation against {tool.output_schema.__name__}; "
                    "treated as a miss",
                    error_class=type(exc).__name__,
                )
            return None
        if emit is not None:
            emit(
                ToolCacheHit,
                agent_name=ctx.agent_name,
                tool_ref=tool.descriptor.ref,
                tool_version=tool.descriptor.version,
                cached_at=hit.cached_at,
            )
        return output

    async def _cache_store(
        self,
        tool: RegisteredTool,
        cache: ResultCache,
        hashed: str,
        output: BaseModel,
        ctx: RunContext,
        emit: EmitFn | None,
    ) -> None:
        assert tool.cache_ttl_s is not None  # guarded by the caller
        key = scoped_input_hash(
            tool.cache_scope, ctx.session.project, ctx.agent_name, hashed
        )
        try:
            await cache.store(
                tool.descriptor.ref,
                tool.descriptor.version,
                key,
                output,
                tool.cache_ttl_s,
            )
        except CacheError as exc:
            if emit is not None:
                emit(
                    WarningEvent,
                    agent_name=ctx.agent_name,
                    category="cache.tool.error",
                    message=f"tool-result cache store failed for "
                    f"{tool.name!r}; result NOT cached (fail-open): {exc}",
                    error_class=type(exc).__name__,
                )
            return
        if emit is not None:
            emit(
                ToolCacheStore,
                agent_name=ctx.agent_name,
                tool_ref=tool.descriptor.ref,
                tool_version=tool.descriptor.version,
                ttl_s=tool.cache_ttl_s,
            )

    async def _run_with_retries(
        self,
        tool: RegisteredTool,
        inputs: BaseModel,
        ctx: RunContext,
        retries: _RetryTracker,
    ) -> BaseModel:
        policy = ctx.retry_policy
        timeout_s = ctx.timeout_s if ctx.timeout_s is not None else tool.timeout_s
        attempt = 0
        auth_retry_used = False
        while True:
            attempt += 1
            # Recorded BEFORE the attempt so the failure path reports how many
            # retries actually ran (a raise after N retries must not report 0).
            retries.count = attempt - 1
            try:
                return await self._one_attempt(tool, inputs, ctx, timeout_s)
            except ConnectionAuthError:
                # on_auth_error refresh: evict + rebuild via the accessor,
                # retry the handler ONCE. A second 401 propagates.
                if (
                    tool.auth_error_retry
                    and not auth_retry_used
                    and ctx.connections is not None
                    and await ctx.connections.on_auth_error()
                ):
                    auth_retry_used = True
                    continue
                raise
            except FoundryError as exc:
                retryable = type(exc).__name__ in policy.retryable_errors
                if not retryable or attempt >= policy.max_attempts:
                    raise
                await asyncio.sleep(policy.delay_for(attempt))
                if ctx.session.cancel_token.cancelled():
                    raise RunCancelled(
                        "run cancelled during tool retry backoff",
                        context={"tool": tool.name,
                                 "reason": ctx.session.cancel_token.reason},
                    ) from exc

    async def _one_attempt(
        self,
        tool: RegisteredTool,
        inputs: BaseModel,
        ctx: RunContext,
        timeout_s: float,
    ) -> BaseModel:
        try:
            async with asyncio.timeout(timeout_s):
                return await tool.handle(inputs, ctx)
        except TimeoutError as exc:
            raise ToolHandlerError(
                f"tool {tool.name!r} exceeded its timeout of {timeout_s}s",
                context={"tool": tool.name, "timeout_s": timeout_s,
                         "cause_type": "TimeoutError"},
                cause=exc,
            ) from exc
        except (
            ToolError,
            FoundryConnectionError,
            ProviderError,
            RunCancelled,
            asyncio.CancelledError,
        ):
            raise
        except Exception as exc:  # wrap: arbitrary exceptions never escape
            raise ToolHandlerError(
                f"tool {tool.name!r} handler raised "
                f"{type(exc).__name__}: {exc}",
                context={"tool": tool.name, "cause_type": type(exc).__name__},
                cause=exc,
            ) from exc

    def _validate_output(self, tool: RegisteredTool, output: Any) -> BaseModel:
        if isinstance(output, tool.output_schema):
            return output
        try:
            return tool.output_schema.model_validate(output)
        except ValidationError as exc:
            raise ToolOutputValidationError(
                f"tool {tool.name!r} returned output that failed validation "
                f"against {tool.output_schema.__name__} "
                f"(got {type(output).__name__})",
                context={
                    "tool": tool.name,
                    "output_schema": tool.output_schema.__name__,
                    "received_type": type(output).__name__,
                },
                cause=exc,
            ) from exc


__all__ = [
    "BackoffStrategy",
    "BaseTool",
    "EmitFn",
    "RegisteredTool",
    "RetryPolicy",
    "RunContext",
    "Tool",
    "ToolDescriptor",
    "ToolHandler",
    "ToolRegistry",
    "input_hash",
    "validate_handler_signature",
]
