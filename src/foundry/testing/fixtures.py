"""Test doubles for project-local pytest (docs/82 § `foundry.testing` fixtures).

Everything here is a plain, deterministic fake built against the ``foundry.core``
protocols — no network, no framework spin-up. ``scripted_transport`` is the one
HTTP-level helper: it lets a fully compiled project run end-to-end against
``httpx.MockTransport`` with scripted provider responses.

This module MUST NOT import pytest (fixtures are plain classes/functions; the
pytest glue lives in ``foundry.testing.pytest_plugin``).
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

import httpx

from foundry.core.connection import (
    AuthScheme,
    Connection,
    ConnectionAccessor,
    ConnectionDescriptor,
    ConnectionHealth,
)
from foundry.core.embedder import EmbedderCapabilities, EmbedderPricing, Embedding
from foundry.core.errors import ConnectionSlotNotDeclaredError, RetrievalError
from foundry.core.messages import FoundryMessage, TextBlock
from foundry.core.model import ModelDelta, ModelResponse
from foundry.core.retrieval import RetrievedDocument, Retriever, RetrieverAccessor
from foundry.core.session import Session
from foundry.core.tool import RetryPolicy, RunContext
from foundry.core.types import CredentialsRef, ResolvedCredentials
from foundry.providers import ResolvedModelSettings, ToolSchema

# --- Connections -------------------------------------------------------------


class MockConnection:
    """A ``foundry.core.connection.Connection`` double wrapping any client.

    Handlers see exactly what ``ctx.connections.get(slot)`` returns at
    runtime: an object with ``ref`` / ``slot`` attributes, a ``client``
    property, and ``async health()``. Assert against the wrapped client
    directly (pass a ``unittest.mock.Mock`` to record method calls).
    """

    def __init__(
        self,
        client: Any,
        descriptor: ConnectionDescriptor | None = None,
        healthy: bool = True,
    ) -> None:
        self._client = client
        self.healthy = healthy
        self.descriptor = descriptor
        self.ref = descriptor.ref if descriptor is not None else "mock/unbound@v1"
        self.slot = descriptor.slot if descriptor is not None else ""
        self.health_calls = 0

    @property
    def client(self) -> Any:
        return self._client

    async def health(self) -> ConnectionHealth:
        self.health_calls += 1
        return ConnectionHealth(
            ok=self.healthy,
            latency_ms=0,
            message="" if self.healthy else "mock connection marked unhealthy",
            checked_at=datetime.now(UTC),
        )

    def bind_slot(self, slot: str) -> None:
        """Fill in the default descriptor once the slot name is known
        (called by ``MockConnectionAccessor``)."""
        if self.descriptor is None:
            self.descriptor = ConnectionDescriptor(
                ref=f"mock/{slot}@v1",
                slot=slot,
                auth_scheme=AuthScheme.API_KEY,
                config_hash="mock",
            )
        self.ref = self.descriptor.ref
        self.slot = self.descriptor.slot


class MockConnectionAccessor:
    """Full ``ConnectionAccessor`` implementation over a slot → MockConnection
    map. Unknown slots raise ``ConnectionSlotNotDeclaredError`` — the same
    error the real per-tool-call accessor raises at ``get`` time."""

    def __init__(self, connections: dict[str, MockConnection] | None = None) -> None:
        self._connections = dict(connections or {})
        for slot, conn in self._connections.items():
            conn.bind_slot(slot)
        self.released = False
        self.auth_error_calls = 0

    def _lookup(self, slot: str) -> MockConnection:
        conn = self._connections.get(slot)
        if conn is None:
            raise ConnectionSlotNotDeclaredError(
                f"tool requested connection slot {slot!r}, which it did not "
                f"declare in connections_required (declared: "
                f"{', '.join(sorted(self._connections)) or '(none)'})",
                context={"slot": slot, "declared_slots": sorted(self._connections)},
            )
        return conn

    async def get(self, slot: str) -> Connection[Any]:
        return self._lookup(slot)

    async def health(self, slot: str) -> ConnectionHealth:
        return await self._lookup(slot).health()

    def descriptor(self, slot: str) -> ConnectionDescriptor:
        descriptor = self._lookup(slot).descriptor
        assert descriptor is not None  # bind_slot ran in __init__
        return descriptor

    async def on_auth_error(self) -> bool:
        self.auth_error_calls += 1
        return False

    async def release_all(self) -> None:
        self.released = True


# --- Retrievers ---------------------------------------------------------------


def _terms(text: str) -> set[str]:
    return {t for t in "".join(c if c.isalnum() else " " for c in text.lower()).split() if t}


def _overlap_score(query: str, text: str) -> float:
    query_terms = _terms(query)
    if not query_terms:
        return 0.0
    return len(query_terms & _terms(text)) / len(query_terms)


class MockRetriever:
    """Deterministic ``Retriever``: scores documents by naive term overlap
    with the query (ties keep input order), applies metadata equality
    ``filters``, returns the ``top_k`` best."""

    def __init__(
        self,
        documents: list[RetrievedDocument],
        name: str = "mock_retriever",
        kind: Literal["dense", "sparse", "hybrid"] = "dense",
    ) -> None:
        self.name = name
        self.kind: Literal["dense", "sparse", "hybrid"] = kind
        self._documents = list(documents)

    async def retrieve(
        self,
        query: str,
        top_k: int = 20,
        filters: dict[str, Any] | None = None,
    ) -> list[RetrievedDocument]:
        candidates = [
            doc
            for doc in self._documents
            if not filters
            or all(doc.metadata.get(key) == value for key, value in filters.items())
        ]
        scored = sorted(
            enumerate(candidates),
            key=lambda pair: (-_overlap_score(query, pair[1].text), pair[0]),
        )
        return [
            doc.model_copy(update={"score": _overlap_score(query, doc.text)})
            for _, doc in scored[:top_k]
        ]


class MockReranker:
    """Deterministic ``Reranker``: reorders by term overlap with the query,
    descending; ties keep the input order (stable)."""

    def __init__(self, name: str = "mock_reranker", model: str = "mock-rerank") -> None:
        self.name = name
        self.model = model

    async def rerank(
        self,
        query: str,
        documents: list[RetrievedDocument],
        top_k: int | None = None,
    ) -> list[RetrievedDocument]:
        scored = sorted(
            enumerate(documents),
            key=lambda pair: (-_overlap_score(query, pair[1].text), pair[0]),
        )
        limit = len(scored) if top_k is None else top_k
        return [
            doc.model_copy(update={"score": _overlap_score(query, doc.text)})
            for _, doc in scored[:limit]
        ]


class MockRetrieverAccessor:
    """Full ``RetrieverAccessor`` over a slot → Retriever map. Unknown slots
    raise ``RetrievalError`` — mirroring ``MappingRetrieverAccessor``."""

    def __init__(self, retrievers: dict[str, Retriever] | None = None) -> None:
        self._retrievers = dict(retrievers or {})

    def get(self, slot: str) -> Retriever:
        retriever = self._retrievers.get(slot)
        if retriever is None:
            raise RetrievalError(
                f"no retriever bound to slot {slot!r}; declared slots: "
                f"{', '.join(sorted(self._retrievers)) or '(none)'}",
                context={"slot": slot, "declared_slots": sorted(self._retrievers)},
            )
        return retriever

    def slots(self) -> list[str]:
        return sorted(self._retrievers)


# --- RunContext ----------------------------------------------------------------


class RunContextFixture:
    """Builder for a real ``RunContext`` (frozen Pydantic model) with test
    defaults, so handler code runs without spinning the framework."""

    def __init__(
        self,
        run_id: str = "test-run",
        agent_name: str = "test_agent",
        tool_ref: str = "local/test_tool@v1",
        connections: ConnectionAccessor | None = None,
        retrievers: RetrieverAccessor | None = None,
        approvals: dict[str, dict[str, Any]] | None = None,
        timeout_s: float | None = None,
        *,
        project: str = "test_project",
        retry_policy: RetryPolicy | None = None,
        session: Session | None = None,
    ) -> None:
        self._run_id = run_id
        self._agent_name = agent_name
        self._tool_ref = tool_ref
        self._connections = connections
        self._retrievers = retrievers
        self._approvals = dict(approvals or {})
        self._timeout_s = timeout_s
        self._project = project
        self._retry_policy = retry_policy or RetryPolicy(
            max_attempts=1, initial_delay_s=0.01, max_delay_s=0.02
        )
        self._session = session

    def build(self) -> RunContext:
        return RunContext(
            run_id=self._run_id,
            agent_name=self._agent_name,
            session=self._session or Session.new(project=self._project),
            tool_ref=self._tool_ref,
            timeout_s=self._timeout_s,
            retry_policy=self._retry_policy,
            connections=self._connections,
            retrievers=self._retrievers,
            approvals=self._approvals,
        )


# --- Provider ------------------------------------------------------------------


class MockProvider:
    """Scripted-response duck-type of the Provider ``generate`` / ``stream``
    surface, for direct agent/unit tests without LLM costs.

    Deliberately NOT a ``ProviderAdapter`` subclass (no credentials/transport
    plumbing). Each call pops the next scripted ``ModelResponse``; when the
    script is exhausted the call fails with ``AssertionError`` so tests stay
    deterministic. Every call's messages are recorded in ``.calls``.
    """

    def __init__(
        self,
        name: str = "mock",
        model: str = "mock-model",
        responses: list[ModelResponse] | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self._responses = list(responses or [])
        self.calls: list[list[FoundryMessage]] = []

    async def generate(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema] | None = None,
        settings: ResolvedModelSettings | None = None,
        session: Session | None = None,
    ) -> ModelResponse:
        _ = tools, settings, session
        self.calls.append(list(messages))
        if not self._responses:
            raise AssertionError(
                "MockProvider exhausted: no scripted responses left "
                f"(served {len(self.calls) - 1} so far)"
            )
        return self._responses.pop(0)

    async def stream(
        self,
        messages: list[FoundryMessage],
        tools: list[ToolSchema] | None = None,
        settings: ResolvedModelSettings | None = None,
        session: Session | None = None,
    ) -> AsyncIterator[ModelDelta]:
        response = await self.generate(messages, tools, settings, session)
        index = 0
        for block in response.message.content:
            if isinstance(block, TextBlock):
                yield ModelDelta(content_block_index=index, delta=block)
                index += 1
        yield ModelDelta(
            content_block_index=index,
            stop_reason=response.stop_reason,
            usage=response.usage,
        )


def scripted_transport(
    payloads: list[dict[str, Any]] | list[str],
    provider: Literal["anthropic", "openai"] = "anthropic",
) -> httpx.MockTransport:
    """``httpx.MockTransport`` returning one well-formed provider chat
    response per successive request, so a compiled project runs end-to-end
    with only the HTTP layer substituted.

    String payloads become a single text block; dict payloads are used as-is
    as the response's content block (e.g. a ``tool_use`` block for the
    anthropic shape). Requests beyond the script raise ``AssertionError``.
    """
    if provider not in ("anthropic", "openai"):
        raise AssertionError(
            f"scripted_transport supports 'anthropic' and 'openai'; got {provider!r}"
        )
    remaining: list[dict[str, Any] | str] = list(payloads)

    def handler(request: httpx.Request) -> httpx.Response:
        _ = request
        if not remaining:
            raise AssertionError(
                "scripted_transport exhausted: no scripted payloads left"
            )
        payload = remaining.pop(0)
        body = (
            _anthropic_body(payload)
            if provider == "anthropic"
            else _openai_body(payload)
        )
        return httpx.Response(200, json=body)

    return httpx.MockTransport(handler)


def _anthropic_body(payload: dict[str, Any] | str) -> dict[str, Any]:
    blocks = (
        [{"type": "text", "text": payload}] if isinstance(payload, str) else [payload]
    )
    stop = "tool_use" if any(b.get("type") == "tool_use" for b in blocks) else "end_turn"
    return {
        "content": blocks,
        "stop_reason": stop,
        "model": "claude-haiku-4-5",
        "usage": {"input_tokens": 50, "output_tokens": 20},
    }


def _openai_body(payload: dict[str, Any] | str) -> dict[str, Any]:
    if isinstance(payload, dict):
        if payload.get("type") != "text":
            raise AssertionError(
                "scripted_transport(provider='openai') supports string payloads "
                "and {'type': 'text', ...} blocks only; got "
                f"{payload.get('type')!r}"
            )
        text = str(payload["text"])
    else:
        text = payload
    return {
        "model": "gpt-4o-mini",
        "choices": [
            {
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 12},
    }


# --- Embedder --------------------------------------------------------------------


class MockEmbedder:
    """Deterministic ``Embedder``: vectors are derived from a hash of the
    input string, so equal inputs embed identically across runs."""

    def __init__(self, dimensions: int = 8, model: str = "mock-embed") -> None:
        self._dimensions = dimensions
        self._model = model

    @property
    def name(self) -> str:
        return f"mock/{self._model}"

    @property
    def model(self) -> str:
        return self._model

    @property
    def capabilities(self) -> EmbedderCapabilities:
        return EmbedderCapabilities(
            provider="mock",
            model=self._model,
            dimensions=self._dimensions,
            max_input_tokens=8192,
            supports_query_document_split=False,
            supports_batch=True,
            max_batch_size=128,
            pricing=EmbedderPricing(input_per_1m=Decimal("0")),
        )

    def _vector(self, text: str) -> list[float]:
        return [
            int.from_bytes(
                hashlib.sha256(f"{index}:{text}".encode()).digest()[:8], "big"
            )
            / 2**64
            for index in range(self._dimensions)
        ]

    async def embed(
        self,
        inputs: list[str],
        purpose: Literal["query", "document"] = "document",
    ) -> list[Embedding]:
        _ = purpose
        return [
            Embedding(
                vector=self._vector(text),
                dimensions=self._dimensions,
                model=self._model,
                input_tokens=max(1, len(text) // 4),
                latency_ms=0,
                cost_estimate_usd=Decimal("0"),
            )
            for text in inputs
        ]


# --- Secrets ---------------------------------------------------------------------


class MockSecretsResolver:
    """``SecretsResolver`` double: every ref resolves to a fake env secret.
    Never put real credential material here."""

    def __init__(self, secret: str = "fake-key-for-tests") -> None:
        self._secret = secret

    def resolve(self, ref: CredentialsRef | None) -> ResolvedCredentials:
        _ = ref
        return ResolvedCredentials(kind="env", secret=self._secret)


__all__ = [
    "MockConnection",
    "MockConnectionAccessor",
    "MockEmbedder",
    "MockProvider",
    "MockReranker",
    "MockRetriever",
    "MockRetrieverAccessor",
    "MockSecretsResolver",
    "RunContextFixture",
    "scripted_transport",
]
