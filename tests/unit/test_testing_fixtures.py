"""foundry.testing fixtures + state helpers (docs/82 § foundry.testing)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from foundry.core import (
    RegisteredTool,
    StateBase,
    ToolDescriptor,
    ToolRegistry,
)
from foundry.core.connection import AuthScheme, ConnectionDescriptor
from foundry.core.errors import ConnectionSlotNotDeclaredError, RetrievalError
from foundry.core.messages import FoundryMessage, MessageRole, TextBlock
from foundry.core.model import ModelResponse, StopReason, TokenUsage
from foundry.core.retrieval import RetrievedDocument
from foundry.core.tool import RunContext
from foundry.testing import (
    MockConnection,
    MockConnectionAccessor,
    MockEmbedder,
    MockProvider,
    MockReranker,
    MockRetriever,
    MockRetrieverAccessor,
    MockSecretsResolver,
    RunContextFixture,
    StateBuilder,
    assert_state_transition,
    make_state,
)

# --- RunContextFixture + connection mocks -------------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, sql: str) -> str:
        self.queries.append(sql)
        return "42"


class LookupIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sql: str


class LookupOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    value: str


@pytest.mark.unit
async def test_run_context_fixture_drives_a_real_dispatch() -> None:
    client = _FakeClient()
    ctx = RunContextFixture(
        agent_name="investigator",
        tool_ref="local/lookup@v1",
        connections=MockConnectionAccessor(
            {"reference_db": MockConnection(client=client)}
        ),
        approvals={"a1": {"decision": "approved", "reason": "looks fine"}},
    ).build()
    assert isinstance(ctx, RunContext)
    assert ctx.run_id == "test-run"
    assert ctx.agent_name == "investigator"

    # approvals accessors work
    assert ctx.approval_resolved("a1")
    assert ctx.approval_decision("a1") == "approved"
    assert ctx.approval_reason("a1") == "looks fine"
    assert not ctx.approval_resolved("a2")

    async def handle(inputs: BaseModel, ctx: RunContext) -> LookupOut:
        assert isinstance(inputs, LookupIn)
        assert ctx.connections is not None
        conn = await ctx.connections.get("reference_db")
        return LookupOut(value=conn.client.query(inputs.sql))

    registry = ToolRegistry()
    registry.register(
        RegisteredTool(
            descriptor=ToolDescriptor(
                name="lookup", ref="local/lookup", version="v1"
            ),
            input_schema=LookupIn,
            output_schema=LookupOut,
            handler=handle,
        )
    )
    out = await registry.dispatch("lookup", ["lookup"], {"sql": "select 42"}, ctx)
    assert isinstance(out, LookupOut) and out.value == "42"
    assert client.queries == ["select 42"]


@pytest.mark.unit
async def test_mock_connection_gets_default_descriptor_per_slot() -> None:
    accessor = MockConnectionAccessor({"db": MockConnection(client=object())})
    descriptor = accessor.descriptor("db")
    assert descriptor.ref == "mock/db@v1"
    assert descriptor.slot == "db"
    assert descriptor.auth_scheme is AuthScheme.API_KEY
    assert descriptor.config_hash == "mock"

    conn = await accessor.get("db")
    assert conn.ref == "mock/db@v1"
    assert conn.slot == "db"


@pytest.mark.unit
async def test_mock_connection_explicit_descriptor_wins() -> None:
    descriptor = ConnectionDescriptor(
        ref="catalog/warehouse@v2",
        slot="reference_db",
        auth_scheme=AuthScheme.BASIC_AUTH,
        config_hash="abc123",
        principal="test-service-account",
    )
    accessor = MockConnectionAccessor(
        {"reference_db": MockConnection(client=object(), descriptor=descriptor)}
    )
    assert accessor.descriptor("reference_db") == descriptor


@pytest.mark.unit
async def test_mock_connection_accessor_unknown_slot_raises() -> None:
    accessor = MockConnectionAccessor({"db": MockConnection(client=object())})
    with pytest.raises(ConnectionSlotNotDeclaredError) as excinfo:
        await accessor.get("nope")
    assert excinfo.value.context["declared_slots"] == ["db"]
    with pytest.raises(ConnectionSlotNotDeclaredError):
        accessor.descriptor("nope")
    with pytest.raises(ConnectionSlotNotDeclaredError):
        await accessor.health("nope")


@pytest.mark.unit
async def test_mock_connection_health_and_lifecycle() -> None:
    accessor = MockConnectionAccessor(
        {
            "up": MockConnection(client=object()),
            "down": MockConnection(client=object(), healthy=False),
        }
    )
    assert (await accessor.health("up")).ok is True
    down = await accessor.health("down")
    assert down.ok is False and down.message

    assert await accessor.on_auth_error() is False
    assert accessor.auth_error_calls == 1
    assert accessor.released is False
    await accessor.release_all()
    assert accessor.released is True


# --- MockProvider ----------------------------------------------------------------


def _response(text: str) -> ModelResponse:
    return ModelResponse(
        message=FoundryMessage(
            role=MessageRole.ASSISTANT, content=[TextBlock(text=text)]
        ),
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(input_tokens=10, output_tokens=5),
        model="mock-model",
        provider="mock",
        latency_ms=1,
    )


def _user(text: str) -> FoundryMessage:
    return FoundryMessage(role=MessageRole.USER, content=[TextBlock(text=text)])


@pytest.mark.unit
async def test_mock_provider_pops_scripted_responses_and_records_calls() -> None:
    provider = MockProvider(responses=[_response("first"), _response("second")])
    r1 = await provider.generate([_user("q1")])
    r2 = await provider.generate([_user("q2")])
    assert isinstance(r1.message.content[0], TextBlock)
    assert r1.message.content[0].text == "first"
    assert isinstance(r2.message.content[0], TextBlock)
    assert r2.message.content[0].text == "second"
    assert len(provider.calls) == 2
    assert isinstance(provider.calls[0][0].content[0], TextBlock)
    assert provider.calls[0][0].content[0].text == "q1"

    with pytest.raises(AssertionError, match="MockProvider exhausted"):
        await provider.generate([_user("q3")])


@pytest.mark.unit
async def test_mock_provider_stream_yields_text_deltas_then_usage() -> None:
    provider = MockProvider(responses=[_response("streamed")])
    deltas = [d async for d in provider.stream([_user("q")])]
    assert len(deltas) == 2
    assert isinstance(deltas[0].delta, TextBlock)
    assert deltas[0].delta.text == "streamed"
    assert deltas[-1].stop_reason is StopReason.END_TURN
    assert deltas[-1].usage is not None
    assert deltas[-1].usage.output_tokens == 5


# --- MockEmbedder ------------------------------------------------------------------


@pytest.mark.unit
async def test_mock_embedder_is_deterministic_with_right_dims() -> None:
    embedder = MockEmbedder(dimensions=16)
    assert embedder.capabilities.dimensions == 16
    assert embedder.model == "mock-embed"
    a1, b = await embedder.embed(["alpha", "beta"])
    (a2,) = await embedder.embed(["alpha"], purpose="query")
    assert a1.dimensions == 16 and len(a1.vector) == 16
    assert a1.vector == a2.vector  # same input, same vector
    assert a1.vector != b.vector  # different input, different vector


# --- MockRetriever / MockReranker ----------------------------------------------------


def _doc(id_: str, text: str, **metadata: Any) -> RetrievedDocument:
    return RetrievedDocument(id=id_, text=text, score=0.0, metadata=metadata)


@pytest.mark.unit
async def test_mock_retriever_orders_by_term_overlap_deterministically() -> None:
    docs = [
        _doc("d1", "settlement failed for equity trade"),
        _doc("d2", "the trade settlement pipeline failed again"),
        _doc("d3", "unrelated marketing copy"),
    ]
    retriever = MockRetriever(docs)
    assert retriever.kind == "dense"
    hits = await retriever.retrieve("settlement pipeline failed", top_k=2)
    assert [h.id for h in hits] == ["d2", "d1"]
    assert hits[0].score == 1.0
    # deterministic across calls
    again = await retriever.retrieve("settlement pipeline failed", top_k=2)
    assert [h.id for h in again] == ["d2", "d1"]


@pytest.mark.unit
async def test_mock_retriever_applies_metadata_filters() -> None:
    docs = [
        _doc("d1", "settlement report", region="emea"),
        _doc("d2", "settlement report", region="apac"),
    ]
    hits = await MockRetriever(docs).retrieve(
        "settlement report", filters={"region": "apac"}
    )
    assert [h.id for h in hits] == ["d2"]


@pytest.mark.unit
async def test_mock_reranker_reorders_stably() -> None:
    docs = [
        _doc("d1", "nothing relevant here"),
        _doc("d2", "late amendment root cause"),
        _doc("d3", "nothing relevant there"),
    ]
    reranked = await MockReranker().rerank("root cause amendment", docs)
    assert [d.id for d in reranked] == ["d2", "d1", "d3"]  # ties keep input order
    top = await MockReranker().rerank("root cause amendment", docs, top_k=1)
    assert [d.id for d in top] == ["d2"]


@pytest.mark.unit
async def test_mock_retriever_accessor_unknown_slot_raises() -> None:
    accessor = MockRetrieverAccessor({"kb": MockRetriever([])})
    assert accessor.get("kb") is not None
    with pytest.raises(RetrievalError) as excinfo:
        accessor.get("nope")
    assert excinfo.value.context["declared_slots"] == ["kb"]


# --- MockSecretsResolver --------------------------------------------------------------


@pytest.mark.unit
def test_mock_secrets_resolver_returns_fake_env_secret() -> None:
    resolved = MockSecretsResolver().resolve(None)
    assert resolved.kind == "env"
    assert resolved.secret == "fake-key-for-tests"
    assert "fake-key-for-tests" not in repr(resolved)  # redact-on-print


# --- make_state / StateBuilder / assert_state_transition -------------------------------


_STATE_YAML = """\
schema:
  messages:
    type: list[str]
    description: transcript so far
  status:
    type: str
    description: current status
  counters:
    type: dict[str, int]
    description: per-category counts
    default: {}
reducers:
  messages: append
  counters: merge
visibility:
  worker:
    read: [messages, status]
    write: [messages, status, counters]
"""


@pytest.fixture
def state_spec(tmp_path: Path) -> Path:
    path = tmp_path / "state.yaml"
    path.write_text(_STATE_YAML)
    return path


@pytest.mark.unit
def test_make_state_builds_a_validated_instance(state_spec: Path) -> None:
    state = make_state(state_spec, messages=["m1"], status="open")
    assert isinstance(state, StateBase)
    assert state.messages == ["m1"]  # type: ignore[attr-defined]
    assert state.status == "open"  # type: ignore[attr-defined]
    assert state.counters == {}  # type: ignore[attr-defined]

    with pytest.raises(ValidationError):
        make_state(state_spec, messages="not-a-list", status="open")


@pytest.mark.unit
def test_state_builder_fluent_set_and_build(state_spec: Path) -> None:
    state = (
        StateBuilder(state_spec)
        .set("messages", ["m1", "m2"])
        .set("status", "triaged")
        .build()
    )
    assert state.messages == ["m1", "m2"]  # type: ignore[attr-defined]
    assert state.status == "triaged"  # type: ignore[attr-defined]


@pytest.mark.unit
def test_assert_state_transition_applies_reducers(state_spec: Path) -> None:
    initial = make_state(state_spec, messages=["m1"], status="open")
    assert_state_transition(
        state_spec,
        initial=initial,
        deltas=[
            {"messages": ["m2"], "counters": {"breaks": 1}},
            {"messages": ["m3"], "status": "done", "counters": {"amends": 2}},
        ],
        expected_final={
            "messages": ["m1", "m2", "m3"],  # APPEND
            "status": "done",  # LAST_WRITE_WINS
            "counters": {"breaks": 1, "amends": 2},  # MERGE
        },
    )


@pytest.mark.unit
def test_assert_state_transition_raises_with_readable_diff(state_spec: Path) -> None:
    with pytest.raises(AssertionError) as excinfo:
        assert_state_transition(
            state_spec,
            initial={"messages": ["m1"], "status": "open"},
            deltas=[{"messages": ["m2"]}],
            expected_final={"messages": ["m1"], "status": "open"},
        )
    text = str(excinfo.value)
    assert "state transition mismatch" in text
    assert "messages" in text
    assert "['m1', 'm2']" in text  # actual value shown
    assert "status:" not in text  # matching fields stay out of the diff
