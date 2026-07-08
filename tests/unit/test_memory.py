"""Memory subsystem units (docs/26 § Test expectations 1-8): layers,
coordinator, prompt assembly, compile-time wiring, config schemas."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from foundry.config import (
    AgentSpec,
    EpisodicMemoryLayerConfig,
    FunctionNodeSpec,
    MemoryConfig,
    MemoryWindow,
    SemanticMemoryLayerConfig,
    WorkingMemoryLayerConfig,
)
from foundry.core import (
    FoundryMessage,
    MemoryConsolidate,
    MemoryContext,
    MemoryContribution,
    MemoryRead,
    MemoryWrite,
    MemoryWriteEvent,
    MessageRole,
    ModelResponse,
    RetrievedDocument,
    RunId,
    Session,
    StopReason,
    TextBlock,
    TokenUsage,
    WarningEvent,
)
from foundry.core.errors import (
    CompileError,
    MemoryConfigError,
    MemoryConsolidateError,
    MemoryLayerError,
    RetrievalError,
)
from foundry.memory import (
    DefaultMemory,
    EpisodicMemoryLayer,
    SemanticMemoryLayer,
    WorkingMemoryLayer,
    prepare_memory,
    weave,
)

# --- fixtures -----------------------------------------------------------------------


def _msg(role: MessageRole, text: str) -> FoundryMessage:
    return FoundryMessage(role=role, content=[TextBlock(text=text)])


def _ctx(
    state_view: dict[str, Any] | None = None,
    *,
    state_writer: Any = None,
    turn_count: int = 0,
    recent: list[FoundryMessage] | None = None,
) -> MemoryContext:
    return MemoryContext(
        run_id=RunId.new(),
        agent_name="agent_a",
        session=Session.new(project="t"),
        state_view=state_view or {},
        state_writer=state_writer,
        turn_count=turn_count,
        recent_messages=recent or [],
    )


class _Emitted:
    def __init__(self) -> None:
        self.events: list[tuple[type, dict[str, Any]]] = []

    def __call__(self, event_cls: type, **fields: Any) -> None:
        self.events.append((event_cls, fields))

    def of(self, cls: type) -> list[dict[str, Any]]:
        return [f for c, f in self.events if c is cls]


class _StubRetriever:
    kind = "sparse"
    name = "stub"

    def __init__(
        self,
        docs: list[RetrievedDocument] | None = None,
        *,
        fail: Exception | None = None,
        with_ingest: bool = False,
    ) -> None:
        self._docs = docs or []
        self._fail = fail
        self.ingested: list[str] = []
        if with_ingest:
            async def ingest(texts: list[str]) -> None:
                self.ingested.extend(texts)

            self.ingest = ingest

    async def retrieve(
        self, query: str, top_k: int = 20, filters: Any = None
    ) -> list[RetrievedDocument]:
        if self._fail is not None:
            raise self._fail
        return self._docs[:top_k]


def _doc(doc_id: str, score: float) -> RetrievedDocument:
    return RetrievedDocument(id=doc_id, text=f"text {doc_id}", score=score)


def _response(text: str, input_tokens: int = 100, output_tokens: int = 20) -> ModelResponse:
    return ModelResponse(
        message=_msg(MessageRole.ASSISTANT, text),
        stop_reason=StopReason.END_TURN,
        usage=TokenUsage(input_tokens=input_tokens, output_tokens=output_tokens),
        model="m",
        provider="fake",
        latency_ms=1,
    )


class _FakeProvider:
    def __init__(self, text: str = "synthesised facts", fail: bool = False) -> None:
        self.text = text
        self.fail = fail
        self.prompts: list[str] = []

    async def generate(
        self, messages: Any, tools: Any, settings: Any = None, session: Any = None
    ) -> ModelResponse:
        self.prompts.append(messages[-1].content[0].text)
        if self.fail:
            raise RuntimeError("provider down")
        return self._respond()

    def _respond(self) -> ModelResponse:
        return _response(self.text)


# --- working layer (docs/26 unit 1) --------------------------------------------------


@pytest.mark.unit
async def test_working_layer_windows_last_n_messages() -> None:
    messages = [_msg(MessageRole.USER, f"m{i}") for i in range(50)]
    layer = WorkingMemoryLayer(
        WorkingMemoryLayerConfig(
            name="short_term", window=MemoryWindow(max_messages=5)
        )
    )
    contribution = await layer.read("q", _ctx({"messages": messages}))
    assert isinstance(contribution.content, list)
    assert [m.content[0].text for m in contribution.content] == [
        "m45", "m46", "m47", "m48", "m49",
    ]


@pytest.mark.unit
async def test_working_layer_windows_by_max_tokens() -> None:
    messages = [_msg(MessageRole.USER, "x" * 400) for _ in range(10)]  # ~100 tok each
    layer = WorkingMemoryLayer(
        WorkingMemoryLayerConfig(
            name="short_term", window=MemoryWindow(max_tokens=250)
        )
    )
    contribution = await layer.read("q", _ctx({"messages": messages}))
    assert len(contribution.content) == 2  # most recent survive
    assert contribution.tokens_estimate <= 250


@pytest.mark.unit
async def test_working_layer_empty_field_contributes_empty() -> None:
    layer = WorkingMemoryLayer(
        WorkingMemoryLayerConfig(
            name="short_term", window=MemoryWindow(max_messages=5)
        )
    )
    contribution = await layer.read("q", _ctx({}))
    assert contribution.content == []
    assert contribution.tokens_estimate == 0


@pytest.mark.unit
async def test_working_layer_string_source_keeps_tail() -> None:
    layer = WorkingMemoryLayer(
        WorkingMemoryLayerConfig(
            name="short_term",
            source_field="transcript",
            window=MemoryWindow(max_tokens=10),
        )
    )
    contribution = await layer.read(
        "q", _ctx({"transcript": "old " * 50 + "RECENT"})
    )
    assert isinstance(contribution.content, str)
    assert contribution.content.endswith("RECENT")
    assert contribution.tokens_estimate <= 10


# --- episodic layer (docs/26 unit 2) --------------------------------------------------


@pytest.mark.unit
async def test_episodic_layer_respects_top_k_and_threshold() -> None:
    docs = [_doc(f"d{i}", score=1.0 - i * 0.1) for i in range(10)]  # 1.0 .. 0.1
    layer = EpisodicMemoryLayer(
        EpisodicMemoryLayerConfig(
            name="past", retriever_slot="s", top_k=5, relevance_threshold=0.7
        ),
        _StubRetriever(docs),
    )
    contribution = await layer.read("q", _ctx())
    assert [d.id for d in contribution.content] == ["d0", "d1", "d2", "d3"]
    assert all(d.score >= 0.7 for d in contribution.content)


@pytest.mark.unit
async def test_episodic_write_ingests_when_supported_and_emits() -> None:
    retriever = _StubRetriever(with_ingest=True)
    emitted = _Emitted()
    layer = EpisodicMemoryLayer(
        EpisodicMemoryLayerConfig(name="past", retriever_slot="s"),
        retriever, emit=emitted, agent_name="agent_a",
    )
    await layer.write(
        MemoryWrite(kind="message", content="user: hi\nassistant: hello"), _ctx()
    )
    assert retriever.ingested == ["user: hi\nassistant: hello"]
    event = emitted.of(MemoryWriteEvent)[0]
    assert event["layer_name"] == "past"
    assert event["write_kind"] == "message"
    assert event["bytes"] > 0


@pytest.mark.unit
async def test_episodic_write_skips_silently_without_ingest() -> None:
    emitted = _Emitted()
    layer = EpisodicMemoryLayer(
        EpisodicMemoryLayerConfig(name="past", retriever_slot="s"),
        _StubRetriever(), emit=emitted,
    )
    await layer.write(MemoryWrite(kind="message", content="hi"), _ctx())
    assert emitted.events == []  # read-only corpus is a valid episodic source


# --- semantic layer (docs/26 units 3 + 4) ----------------------------------------------


@pytest.mark.unit
async def test_semantic_layer_reads_state_field() -> None:
    layer = SemanticMemoryLayer(
        SemanticMemoryLayerConfig(name="facts", state_field="user_facts")
    )
    contribution = await layer.read("q", _ctx({"user_facts": "likes jazz"}))
    assert contribution.content == "likes jazz"
    assert contribution.layer_kind == "semantic"


@pytest.mark.unit
async def test_semantic_consolidator_writes_state_and_emits_token_counts() -> None:
    provider = _FakeProvider("- name: Sam")
    emitted = _Emitted()
    writes: dict[str, str] = {}
    layer = SemanticMemoryLayer(
        SemanticMemoryLayerConfig(
            name="facts",
            state_field="user_facts",
            consolidate_every_n_turns=3,
            consolidator_prompt="prompts/c_v1.md",
        ),
        consolidator_prompt_text="Current: {current}\nRecent: {recent_messages}",
        provider=provider,
        emit=emitted,
        agent_name="agent_a",
    )
    ctx = _ctx(
        {"user_facts": "old facts"},
        state_writer=lambda f, v: writes.__setitem__(f, v),
        turn_count=3,
        recent=[_msg(MessageRole.USER, "my name is Sam")],
    )
    assert layer.consolidation_due(3) and not layer.consolidation_due(2)
    await layer.consolidate(ctx)
    assert writes == {"user_facts": "- name: Sam"}
    assert "old facts" in provider.prompts[0]
    assert "my name is Sam" in provider.prompts[0]
    event = emitted.of(MemoryConsolidate)[0]
    assert event["trigger"] == "periodic"
    assert event["input_tokens_summarised"] == 100
    assert event["output_tokens_written"] == 20


@pytest.mark.unit
async def test_semantic_consolidator_failure_raises_memory_consolidate_error() -> None:
    layer = SemanticMemoryLayer(
        SemanticMemoryLayerConfig(
            name="facts", state_field="user_facts",
            consolidate_every_n_turns=1, consolidator_prompt="prompts/c.md",
        ),
        consolidator_prompt_text="{current} {recent_messages}",
        provider=_FakeProvider(fail=True),
    )
    with pytest.raises(MemoryConsolidateError):
        await layer.consolidate(_ctx({}, state_writer=lambda f, v: None, turn_count=1))


# --- coordinator (docs/26 units 6-8) -----------------------------------------------------


def _working(name: str = "short_term") -> WorkingMemoryLayer:
    return WorkingMemoryLayer(
        WorkingMemoryLayerConfig(name=name, window=MemoryWindow(max_messages=5))
    )


def _episodic_failing(name: str = "past") -> EpisodicMemoryLayer:
    return EpisodicMemoryLayer(
        EpisodicMemoryLayerConfig(name=name, retriever_slot="s"),
        _StubRetriever(fail=RetrievalError("store down")),
    )


def _config(*layers: Any, **kwargs: Any) -> MemoryConfig:
    return MemoryConfig(layers=list(layers), **kwargs)


@pytest.mark.unit
async def test_coordinator_degrades_failed_layer_and_run_continues() -> None:
    emitted = _Emitted()
    working_config = WorkingMemoryLayerConfig(
        name="short_term", window=MemoryWindow(max_messages=5)
    )
    episodic_config = EpisodicMemoryLayerConfig(name="past", retriever_slot="s")
    memory = DefaultMemory(
        [_working(), _episodic_failing()],
        config=_config(working_config, episodic_config),
        emit=emitted,
        agent_name="agent_a",
    )
    envelope = await memory.read(
        "q", _ctx({"messages": [_msg(MessageRole.USER, "hi")]})
    )
    assert envelope.layers_failed == ["past"]
    by_name = {c.layer_name: c for c in envelope.contributions}
    assert by_name["past"].content == []          # empty contribution
    assert by_name["short_term"].tokens_estimate > 0   # others unaffected
    warning = emitted.of(WarningEvent)[0]
    assert warning["category"] == "memory.layer_failed"
    assert "'past'" in warning["message"]
    read_event = emitted.of(MemoryRead)[0]
    assert read_event["layers_failed"] == ["past"]
    assert read_event["layers_read"] == ["short_term"]


@pytest.mark.unit
async def test_coordinator_fail_strict_raises_memory_layer_error() -> None:
    working_config = WorkingMemoryLayerConfig(
        name="short_term", window=MemoryWindow(max_messages=5)
    )
    episodic_config = EpisodicMemoryLayerConfig(name="past", retriever_slot="s")
    memory = DefaultMemory(
        [_working(), _episodic_failing()],
        config=_config(working_config, episodic_config, fail_strict=True),
    )
    with pytest.raises(MemoryLayerError) as excinfo:
        await memory.read("q", _ctx({"messages": []}))
    assert "'past'" in str(excinfo.value)
    assert "store down" in str(excinfo.value)


@pytest.mark.unit
async def test_envelope_cap_truncates_last_listed_layer_first() -> None:
    emitted = _Emitted()
    first = SemanticMemoryLayer(
        SemanticMemoryLayerConfig(name="first", state_field="a")
    )
    last = SemanticMemoryLayer(
        SemanticMemoryLayerConfig(name="last", state_field="b")
    )
    memory = DefaultMemory(
        [first, last],
        config=_config(
            SemanticMemoryLayerConfig(name="first", state_field="a"),
            SemanticMemoryLayerConfig(name="last", state_field="b"),
            max_envelope_tokens=150,
        ),
        emit=emitted,
    )
    envelope = await memory.read(
        "q", _ctx({"a": "x" * 400, "b": "y" * 400})  # ~100 tokens each
    )
    assert envelope.truncated is True
    assert envelope.layers_truncated == ["last"]   # last-listed first
    by_name = {c.layer_name: c for c in envelope.contributions}
    assert by_name["first"].tokens_estimate == 100  # untouched
    assert by_name["last"].tokens_estimate <= 50
    assert envelope.total_tokens_estimate <= 150
    read_event = emitted.of(MemoryRead)[0]
    assert read_event["truncated"] is True
    assert read_event["layers_truncated"] == ["last"]


@pytest.mark.unit
async def test_coordinator_consolidate_fail_open_preserves_synthesis() -> None:
    emitted = _Emitted()
    config = SemanticMemoryLayerConfig(
        name="facts", state_field="user_facts",
        consolidate_every_n_turns=1, consolidator_prompt="prompts/c.md",
    )
    layer = SemanticMemoryLayer(
        config,
        consolidator_prompt_text="{current}",
        provider=_FakeProvider(fail=True),
    )
    memory = DefaultMemory([layer], config=_config(config), emit=emitted)
    writes: dict[str, str] = {}
    await memory.consolidate(
        _ctx({"user_facts": "prior"},
             state_writer=lambda f, v: writes.__setitem__(f, v), turn_count=1)
    )
    assert writes == {}  # prior synthesis preserved
    warning = emitted.of(WarningEvent)[0]
    assert warning["category"] == "memory.consolidate_failed"

    strict = DefaultMemory(
        [layer], config=_config(config, fail_strict=True), emit=emitted
    )
    with pytest.raises(MemoryConsolidateError):
        await strict.consolidate(
            _ctx({}, state_writer=lambda f, v: None, turn_count=1)
        )


# --- prompt assembly (docs/26 § Prompt assembly) -------------------------------------


def _contribution(
    name: str, kind: str, content: Any, tokens: int = 10
) -> MemoryContribution:
    return MemoryContribution(
        layer_name=name, layer_kind=kind, content=content, tokens_estimate=tokens
    )


def _envelope(*contributions: MemoryContribution) -> Any:
    from foundry.core import MemoryEnvelope

    return MemoryEnvelope(
        contributions=list(contributions),
        total_tokens_estimate=sum(c.tokens_estimate for c in contributions),
    )


@pytest.mark.unit
def test_weave_default_placements_per_kind() -> None:
    config = MemoryConfig(layers=[
        WorkingMemoryLayerConfig(
            name="short_term", window=MemoryWindow(max_messages=5)
        ),
        EpisodicMemoryLayerConfig(name="past", retriever_slot="s"),
        SemanticMemoryLayerConfig(name="facts", state_field="f"),
    ])
    envelope = _envelope(
        _contribution("short_term", "working", [_msg(MessageRole.USER, "hi")]),
        _contribution("past", "episodic", [_doc("d1", 0.9)]),
        _contribution("facts", "semantic", "likes jazz"),
    )
    woven = weave("HAND-AUTHORED PROMPT", envelope, config)
    # semantic → system_prefix (before), episodic → system_suffix (after)
    prefix, _, suffix = woven.system_text.partition("HAND-AUTHORED PROMPT")
    assert "Persistent context:\nlikes jazz" in prefix
    assert "Relevant past context:\n[d1] text d1" in suffix
    # working → messages
    assert [m.content[0].text for m in woven.memory_messages] == ["hi"]
    assert woven.user_prefix == ""


@pytest.mark.unit
def test_weave_user_message_prefix_wraps_typed_boundary() -> None:
    config = MemoryConfig(
        layers=[SemanticMemoryLayerConfig(name="facts", state_field="f")],
        inject_into_prompt=[{
            "layer": "facts", "placement": "user_message_prefix",
            "template": "{content}",
        }],
    )
    woven = weave(
        "P", _envelope(_contribution("facts", "semantic", "likes jazz")), config
    )
    assert woven.user_prefix == (
        '<memory layer="facts" kind="semantic">\nlikes jazz\n</memory>'
    )
    assert woven.system_text == "P"


@pytest.mark.unit
def test_weave_per_rule_max_tokens_truncates_and_empty_skipped() -> None:
    config = MemoryConfig(
        layers=[
            SemanticMemoryLayerConfig(name="facts", state_field="f"),
            EpisodicMemoryLayerConfig(name="past", retriever_slot="s"),
        ],
        inject_into_prompt=[
            {"layer": "facts", "placement": "system_prefix",
             "template": "{content}", "max_tokens": 5},
            {"layer": "past", "placement": "system_suffix"},
        ],
    )
    envelope = _envelope(
        _contribution("facts", "semantic", "z" * 400, tokens=100),
        _contribution("past", "episodic", [], tokens=0),  # empty → skipped
    )
    woven = weave("P", envelope, config)
    prefix = woven.system_text.split("P")[0]
    assert len(prefix) <= 5 * 4 + 4  # truncated to ~5 tokens
    assert "Relevant past context" not in woven.system_text


# --- wiring validation (docs/26 § Validation rules) ------------------------------------


_AGENT: dict[str, Any] = {
    "name": "agent_a",
    "model_binding": {"provider": "anthropic", "model": "claude-haiku-4-5"},
    "prompt": {"version": "v1", "path": "prompts/v1.md"},
    "output": {"schema": "output_schema.py::Out"},
    "state_visibility": {
        "read": ["messages", "user_facts", "turns"],
        "write": ["messages", "user_facts", "reply"],
    },
    "retrievers": [
        {"slot": "episodes", "ref": "local/episode_store", "version": "v1"}
    ],
}

_FIELDS = {
    "messages": "list[FoundryMessage]",
    "user_facts": "str | None",
    "turns": "list[str]",
    "reply": "str | None",
    "secret_notes": "str",
}


def _prepare(memory: dict[str, Any], tmp_path: Path) -> Any:
    spec = AgentSpec.model_validate({**_AGENT, "memory": memory})
    return prepare_memory(
        spec,
        agent_dir=tmp_path,
        state_field_types=_FIELDS,
        read_scope=["messages", "user_facts", "turns"],
        write_scope=["messages", "user_facts", "reply"],
    )


@pytest.mark.unit
def test_prepare_memory_none_when_memory_off(tmp_path: Path) -> None:
    spec = AgentSpec.model_validate(_AGENT)
    assert prepare_memory(
        spec, agent_dir=tmp_path, state_field_types=_FIELDS,
        read_scope=[], write_scope=[],
    ) is None


@pytest.mark.unit
def test_working_source_field_missing_from_schema_is_config_error(
    tmp_path: Path,
) -> None:
    with pytest.raises(MemoryConfigError) as excinfo:
        _prepare({"layers": [{
            "kind": "working", "name": "w", "source_field": "nope",
            "window": {"max_messages": 5},
        }]}, tmp_path)
    assert "'nope'" in str(excinfo.value)


@pytest.mark.unit
def test_working_source_field_wrong_type_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(MemoryConfigError) as excinfo:
        _prepare({"layers": [{
            "kind": "working", "name": "w", "source_field": "turns",
            "window": {"max_messages": 5},
        }]}, tmp_path)
    assert "list[FoundryMessage]" in str(excinfo.value)


@pytest.mark.unit
def test_layer_field_outside_read_scope_is_compile_error(tmp_path: Path) -> None:
    with pytest.raises(CompileError) as excinfo:
        _prepare({"layers": [{
            "kind": "semantic", "name": "s", "state_field": "secret_notes",
        }]}, tmp_path)
    assert "cannot read" in str(excinfo.value)


@pytest.mark.unit
def test_semantic_field_outside_write_scope_is_compile_error(tmp_path: Path) -> None:
    spec = AgentSpec.model_validate({**_AGENT, "memory": {"layers": [
        {"kind": "semantic", "name": "s", "state_field": "user_facts"},
    ]}})
    with pytest.raises(CompileError) as excinfo:
        prepare_memory(
            spec, agent_dir=tmp_path, state_field_types=_FIELDS,
            read_scope=["user_facts"], write_scope=["reply"],
        )
    assert "cannot write" in str(excinfo.value)


@pytest.mark.unit
def test_episodic_unbound_retriever_slot_is_compile_error(tmp_path: Path) -> None:
    with pytest.raises(CompileError) as excinfo:
        _prepare({"layers": [{
            "kind": "episodic", "name": "e", "retriever_slot": "unbound",
        }]}, tmp_path)
    assert "'unbound'" in str(excinfo.value)
    assert "episodes" in str(excinfo.value)  # names the bound slots


@pytest.mark.unit
def test_missing_consolidator_prompt_on_disk_is_config_error(tmp_path: Path) -> None:
    with pytest.raises(MemoryConfigError) as excinfo:
        _prepare({"layers": [{
            "kind": "semantic", "name": "s", "state_field": "user_facts",
            "consolidate_every_n_turns": 3,
            "consolidator_prompt": "prompts/missing.md",
        }]}, tmp_path)
    assert "missing.md" in str(excinfo.value)


@pytest.mark.unit
def test_prepare_memory_loads_consolidator_prompt_text(tmp_path: Path) -> None:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "c_v1.md").write_text("Summarise {current}")
    prepared = _prepare({"layers": [{
        "kind": "semantic", "name": "facts", "state_field": "user_facts",
        "consolidate_every_n_turns": 3, "consolidator_prompt": "prompts/c_v1.md",
    }]}, tmp_path)
    assert prepared is not None
    assert prepared.consolidator_prompts == {"facts": "Summarise {current}"}


# --- config schemas -----------------------------------------------------------------


@pytest.mark.unit
def test_duplicate_layer_names_rejected_naming_the_duplicate() -> None:
    with pytest.raises(ValidationError) as excinfo:
        MemoryConfig.model_validate({"layers": [
            {"kind": "working", "name": "dup", "window": {"max_messages": 5}},
            {"kind": "semantic", "name": "dup", "state_field": "f"},
        ]})
    assert "dup" in str(excinfo.value)
    assert "unique" in str(excinfo.value)


@pytest.mark.unit
def test_injection_rule_referencing_unknown_layer_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        MemoryConfig.model_validate({
            "layers": [{"kind": "semantic", "name": "facts", "state_field": "f"}],
            "inject_into_prompt": [{"layer": "ghost", "placement": "system_prefix"}],
        })
    assert "ghost" in str(excinfo.value)


@pytest.mark.unit
def test_memory_window_requires_exactly_one_bound() -> None:
    with pytest.raises(ValidationError):
        MemoryWindow.model_validate({})
    with pytest.raises(ValidationError):
        MemoryWindow.model_validate({"max_messages": 5, "max_tokens": 100})


@pytest.mark.unit
def test_semantic_trigger_without_consolidator_prompt_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        SemanticMemoryLayerConfig.model_validate({
            "name": "facts", "state_field": "f", "consolidate_every_n_turns": 3,
        })
    assert "consolidator_prompt" in str(excinfo.value)


@pytest.mark.unit
def test_agent_spec_memory_defaults_to_none() -> None:
    assert AgentSpec.model_validate(_AGENT).memory is None


@pytest.mark.unit
def test_function_node_spec_has_no_llm_surface() -> None:
    """docs/03 § 2c deliverable 1: FunctionNodeSpec must NOT have
    model_binding / tools / iteration_limit (nor memory / semantic_cache)."""
    for llm_field in ("model_binding", "tools", "iteration_limit",
                      "memory", "semantic_cache", "prompt", "output"):
        assert llm_field not in FunctionNodeSpec.model_fields
    for kept in ("function", "state_visibility", "retry_policy", "timeout_s"):
        assert kept in FunctionNodeSpec.model_fields
