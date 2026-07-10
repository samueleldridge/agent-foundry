"""Phase 2c exit-gate integration tests against projects/memory_hello.

Same posture as the 2a/2b suites: no live API keys — the full real path
(compile, function nodes, memory layers, episodic retrieval, consolidation)
runs with only the HTTP layer replaced by httpx.MockTransport serving
api.anthropic.com.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from foundry.cli.run import execute_run
from foundry.core.errors import CompileError
from foundry.runtime.langgraph_adapter import compile_project

REPO_ROOT = Path(__file__).resolve().parents[2]
MEMORY_DIR = REPO_ROOT / "projects" / "memory_hello"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


class MemoryTransport:
    """Scripted anthropic fake: JSON replies for agent turns, Markdown for
    consolidator calls (recognised by the consolidator prompt heading)."""

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.agent_turns = 0
        self.consolidations = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.anthropic.com"
        body = json.loads(request.content)
        self.requests.append(body)
        user_text = body["messages"][-1]["content"][0]["text"]
        if "user_facts consolidator" in user_text:
            self.consolidations += 1
            content = [{
                "type": "text",
                "text": f"- FACTS v{self.consolidations}: name Sam, Paris trip",
            }]
        else:
            self.agent_turns += 1
            content = [{
                "type": "text",
                "text": json.dumps({"reply": f"ack-{self.agent_turns}"}),
            }]
        return httpx.Response(200, json={
            "content": content, "stop_reason": "end_turn",
            "model": "claude-haiku-4-5",
            "usage": {"input_tokens": 120, "output_tokens": 25},
        })

    def build(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def agent_requests(self) -> list[dict[str, Any]]:
        return [
            r for r in self.requests
            if "user_facts consolidator"
            not in r["messages"][-1]["content"][0]["text"]
        ]


def _turns(n: int, suffix: str = "") -> str:
    return json.dumps({
        "raw_turns": [f"  turn-{i:02d} {suffix}".rstrip() + "  "
                      for i in range(1, n + 1)]
    })


def _run_dirs(tmp_path: Path) -> list[Path]:
    root = tmp_path / "foundry_home" / "runs"
    return sorted(root.iterdir()) if root.exists() else []


def _events(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]


def _by_event(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    return [e for e in events if e["event"] == name]


def _final_state(run_dir: Path) -> dict[str, Any]:
    return json.loads((run_dir / "final_state.json").read_text())["state"]


def _copy_project(tmp_path: Path, name: str) -> Path:
    project = tmp_path / name
    shutil.copytree(MEMORY_DIR, project)
    return project


# --- memory: working window (exit gate 1) ---------------------------------------------


@pytest.mark.integration
def test_working_window_shows_exactly_last_5_messages_on_10_turn_run(
    tmp_path: Path,
) -> None:
    transport = MemoryTransport()
    assert execute_run(MEMORY_DIR, _turns(10), transport=transport.build()) == 0
    assert transport.agent_turns == 10

    # Turn 10's request: exactly 5 windowed messages + the current user turn.
    last = transport.agent_requests()[-1]
    chat = last["messages"]
    assert len(chat) == 6
    texts = [m["content"][0]["text"] for m in chat]
    roles = [m["role"] for m in chat]
    # Last 5 of the 18 pre-turn state messages: a7, u8, a8, u9, a9.
    assert roles == ["assistant", "user", "assistant", "user", "assistant", "user"]
    assert texts[0] == json.dumps({"reply": "ack-7"})
    assert texts[1] == "turn-08"
    assert texts[4] == json.dumps({"reply": "ack-9"})
    assert texts[5] == "turn-10"  # the current turn, not from the window

    # And the final state accumulated all 20 messages (10 user + 10 assistant).
    run_dir = _run_dirs(tmp_path)[-1]
    assert len(_final_state(run_dir)["messages"]) == 20


# --- memory: episodic layer (exit gate 2) ----------------------------------------------


@pytest.mark.integration
def test_episodic_snippets_land_in_system_suffix_and_memory_read_lists_layer(
    tmp_path: Path,
) -> None:
    transport = MemoryTransport()
    code = execute_run(
        MEMORY_DIR,
        json.dumps({"raw_turns": ["planning a Paris trip"]}),
        transport=transport.build(),
    )
    assert code == 0

    request = transport.agent_requests()[0]
    system = request["system"]
    # The seeded EP-001 episode mentions Paris → retrieved into the
    # configured system_suffix placement, AFTER the hand-authored prompt.
    assert "Relevant past context:" in system
    assert "[EP-001]" in system
    assert system.index("hello_agent — system prompt v1") < system.index("[EP-001]")

    events = _events(_run_dirs(tmp_path)[-1])
    read = _by_event(events, "memory.read")[0]
    assert "past_sessions" in read["layers_read"]
    assert read["layers_failed"] == []
    # per-turn ingest: episodic write audited
    writes = _by_event(events, "memory.write")
    assert writes and writes[0]["layer_name"] == "past_sessions"


# --- memory: semantic consolidation (exit gate 3) --------------------------------------


@pytest.mark.integration
def test_semantic_consolidation_every_3_turns_writes_state_field(
    tmp_path: Path,
) -> None:
    transport = MemoryTransport()
    assert execute_run(MEMORY_DIR, _turns(10), transport=transport.build()) == 0
    assert transport.consolidations == 3  # turns 3, 6, 9

    run_dir = _run_dirs(tmp_path)[-1]
    events = _events(run_dir)
    consolidate = _by_event(events, "memory.consolidate")
    assert len(consolidate) == 3
    for event in consolidate:
        assert event["layer_name"] == "user_facts"
        assert event["trigger"] == "periodic"
        assert event["input_tokens_summarised"] == 120
        assert event["output_tokens_written"] == 25

    # Synthesised content landed in the configured state field.
    assert _final_state(run_dir)["user_facts"] == (
        "- FACTS v3: name Sam, Paris trip"
    )
    # And turn 4 onwards sees it in the system_prefix placement.
    turn_4 = transport.agent_requests()[3]
    assert "FACTS v1" in turn_4["system"]
    assert turn_4["system"].index("FACTS v1") < turn_4["system"].index(
        "hello_agent — system prompt v1"
    )


# --- memory: degrade-gracefully vs fail-strict (exit gates 4 + 5) -----------------------


def _break_episodic(project: Path) -> None:
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text().replace(
            "corpus_path: episodes.json", "corpus_path: missing.json"
        )
    )


@pytest.mark.integration
def test_failed_episodic_retriever_degrades_with_warning_and_run_completes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_project(tmp_path, "memory_degrade")
    _break_episodic(project)
    transport = MemoryTransport()
    code = execute_run(
        project, json.dumps({"raw_turns": ["hello there"]}),
        transport=transport.build(),
    )
    assert code == 0  # the run completes
    printed = json.loads(capsys.readouterr().out)
    assert printed["formatted_reply"] == "[memory_hello] ack-1"

    events = _events(_run_dirs(tmp_path)[-1])
    warnings = [e for e in _by_event(events, "warning")
                if e["category"] == "memory.layer_failed"]
    assert warnings and "'past_sessions'" in warnings[0]["message"]
    read = _by_event(events, "memory.read")[0]
    assert read["layers_failed"] == ["past_sessions"]
    # the prompt carries NO episodic snippets — the layer contributed nothing
    assert "Relevant past context" not in transport.agent_requests()[0]["system"]


@pytest.mark.integration
def test_fail_strict_aborts_run_with_memory_layer_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_project(tmp_path, "memory_strict")
    _break_episodic(project)
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text().replace("fail_strict: false", "fail_strict: true")
    )
    transport = MemoryTransport()
    code = execute_run(
        project, json.dumps({"raw_turns": ["hello there"]}),
        transport=transport.build(),
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "MemoryLayerError" in err
    assert "past_sessions" in err
    events = _events(_run_dirs(tmp_path)[-1])
    assert _by_event(events, "run.failed")
    assert transport.agent_turns == 0  # aborted before the LLM ran


# --- memory: envelope token cap (exit gate 6) -------------------------------------------


@pytest.mark.integration
def test_envelope_cap_truncates_last_listed_layer_first(tmp_path: Path) -> None:
    project = _copy_project(tmp_path, "memory_cap")
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text().replace(
            "max_envelope_tokens: 4000", "max_envelope_tokens: 100"
        )
    )
    transport = MemoryTransport()
    long_topic = "the Paris trip plan " * 30
    code = execute_run(
        project,
        json.dumps({"raw_turns": [f"turn one about {long_topic}",
                                  f"turn two about {long_topic}"]}),
        transport=transport.build(),
    )
    assert code == 0
    events = _events(_run_dirs(tmp_path)[-1])
    reads = _by_event(events, "memory.read")
    truncated = [e for e in reads if e["truncated"]]
    assert truncated, "the cap must trigger"
    # last-listed layer (past_sessions, the episodic layer) truncates FIRST
    assert truncated[-1]["layers_truncated"][0] == "past_sessions"
    assert truncated[-1]["total_tokens_estimate"] <= 100


# --- memory: layer-name uniqueness (exit gate 7, adversarial) ----------------------------


@pytest.mark.integration
def test_duplicate_layer_names_fail_at_load(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_project(tmp_path, "memory_dup")
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    agent_yaml.write_text(agent_yaml.read_text().replace(
        "    - kind: episodic\n      name: past_sessions\n",
        "    - kind: episodic\n      name: short_term\n",
    ))
    code = execute_run(project, _turns(1), transport=MemoryTransport().build())
    assert code == 2
    err = capsys.readouterr().err
    assert "ConfigValidationError" in err
    assert "short_term" in err and "unique" in err
    assert not _run_dirs(tmp_path)  # load-time: no artifact


# --- FunctionNode end-to-end (exit gate 8) ------------------------------------------------


@pytest.mark.integration
def test_sequential_flow_runs_functions_and_agent_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = MemoryTransport()
    code = execute_run(
        MEMORY_DIR,
        json.dumps({"raw_turns": ["  hi, my name is Sam  ", "", "  bye  "]}),
        transport=transport.build(),
    )
    assert code == 0
    events = _events(_run_dirs(tmp_path)[-1])

    completed = _by_event(events, "function_node.completed")
    assert [e["node_name"] for e in completed] == [
        "normalize_input", "format_output",
    ]
    assert len(_by_event(events, "agent.completed")) == 1
    # order: normalize → agent → format
    order = [e["event"] for e in events
             if e["event"] in ("function_node.completed", "agent.completed")]
    assert order == ["function_node.completed", "agent.completed",
                     "function_node.completed"]

    state = _final_state(_run_dirs(tmp_path)[-1])
    assert state["turns"] == ["hi, my name is Sam", "bye"]  # normalised
    assert transport.agent_turns == 2  # empty turn dropped by normalize_input
    assert state["reply"] == "ack-2"
    assert state["formatted_reply"] == "[memory_hello] ack-2"
    printed = json.loads(capsys.readouterr().out)
    assert printed["formatted_reply"] == "[memory_hello] ack-2"


# --- FunctionNode state visibility (exit gate 9) ------------------------------------------


@pytest.mark.integration
def test_function_out_of_scope_write_dropped_with_warning(tmp_path: Path) -> None:
    project = _copy_project(tmp_path, "memory_oosw")
    function_py = project / "functions" / "format_output" / "function.py"
    function_py.write_text(
        function_py.read_text().replace(
            'return {"formatted_reply": f"[memory_hello] {reply}"}',
            'return {"reply": "HACKED", '
            '"formatted_reply": f"[memory_hello] {reply}"}',
        )
    )
    transport = MemoryTransport()
    assert execute_run(project, _turns(1), transport=transport.build()) == 0

    run_dir = _run_dirs(tmp_path)[-1]
    events = _events(run_dir)
    warnings = [e for e in _by_event(events, "warning")
                if e["category"] == "function_node.out_of_scope_write"]
    assert warnings and "reply" in warnings[0]["message"]
    completed = [e for e in _by_event(events, "function_node.completed")
                 if e["node_name"] == "format_output"]
    assert completed[0]["fields_written"] == ["formatted_reply"]  # only in-scope

    state = _final_state(run_dir)
    assert state["reply"] == "ack-1"  # NOT "HACKED" — out-of-scope write dropped
    assert state["formatted_reply"] == "[memory_hello] ack-1"


# --- FunctionNode observability (exit gate 10) --------------------------------------------


@pytest.mark.integration
def test_function_node_events_carry_full_telemetry(tmp_path: Path) -> None:
    transport = MemoryTransport()
    assert execute_run(MEMORY_DIR, _turns(1), transport=transport.build()) == 0
    events = _events(_run_dirs(tmp_path)[-1])

    started = _by_event(events, "function_node.started")
    completed = _by_event(events, "function_node.completed")
    assert len(started) == 2 and len(completed) == 2
    for event in started:
        assert event["node_name"]
        assert event["node_version"]  # content hash, non-empty
    for event in completed:
        assert event["node_name"]
        assert event["node_version"]
        assert event["fields_written"]
        assert event["bytes_delta"] > 0
        assert event["latency_ms"] >= 0
        assert event["run_id"] == _run_dirs(tmp_path)[-1].name


# --- node namespace collision (exit gate 11, adversarial) ----------------------------------


@pytest.mark.integration
def test_agent_and_function_with_same_name_is_compile_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_project(tmp_path, "memory_collide")
    clone = project / "functions" / "hello_agent"
    shutil.copytree(project / "functions" / "normalize_input", clone)
    function_yaml = clone / "function.yaml"
    function_yaml.write_text(
        function_yaml.read_text().replace(
            "name: normalize_input", "name: hello_agent"
        )
    )
    system_yaml = project / "system.yaml"
    system_yaml.write_text(system_yaml.read_text().replace(
        "functions: [normalize_input, format_output]",
        "functions: [normalize_input, format_output, hello_agent]",
    ))
    code = execute_run(project, _turns(1), transport=MemoryTransport().build())
    assert code == 2
    err = capsys.readouterr().err
    assert "CompileError" in err
    assert "hello_agent" in err
    assert "collision" in err
    assert not _run_dirs(tmp_path)


@pytest.mark.integration
def test_function_named_like_agent_subnode_is_compile_error_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A function named <agent>__llm collides with the agent's reserved
    internal sub-node names — compile-time CompileError, exit 2 (Phase 3
    review finding 4: previously failed only at runtime graph wiring)."""
    project = _copy_project(tmp_path, "memory_reserved")
    clone = project / "functions" / "hello_agent__llm"
    shutil.copytree(project / "functions" / "normalize_input", clone)
    function_yaml = clone / "function.yaml"
    function_yaml.write_text(
        function_yaml.read_text().replace(
            "name: normalize_input", "name: hello_agent__llm"
        )
    )
    system_yaml = project / "system.yaml"
    system_yaml.write_text(system_yaml.read_text().replace(
        "functions: [normalize_input, format_output]",
        "functions: [normalize_input, format_output, hello_agent__llm]",
    ))
    code = execute_run(project, _turns(1), transport=MemoryTransport().build())
    assert code == 2
    err = capsys.readouterr().err
    assert "CompileError" in err
    assert "hello_agent__llm" in err
    assert "reserved" in err
    assert not _run_dirs(tmp_path)


# --- mixed-flow validation (exit gate 12) ----------------------------------------------


@pytest.mark.integration
def test_sequential_flow_with_missing_ref_is_compile_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_project(tmp_path, "memory_ghost")
    system_yaml = project / "system.yaml"
    system_yaml.write_text(system_yaml.read_text().replace(
        "steps: [normalize_input, hello_agent, format_output]",
        "steps: [normalize_input, ghost_step, hello_agent, format_output]",
    ))
    code = execute_run(project, _turns(1), transport=MemoryTransport().build())
    assert code == 2
    err = capsys.readouterr().err
    assert "CompileError" in err
    assert "ghost_step" in err
    assert not _run_dirs(tmp_path)


_GRAPH_FLOW_OK = """flow:
  type: graph
  start: normalize_input
  edges:
    - {from: normalize_input, to: hello_agent}
    - {from: hello_agent, to: format_output}
"""

_GRAPH_FLOW_DANGLING = """flow:
  type: graph
  start: normalize_input
  edges:
    - {from: normalize_input, to: hello_agent}
    - {from: hello_agent, to: ghost_node}
"""

_SEQ_FLOW = """flow:
  type: sequential
  steps: [normalize_input, hello_agent, format_output]
"""


@pytest.mark.integration
def test_graph_flow_refs_resolve_across_agents_and_functions(
    tmp_path: Path,
) -> None:
    """A graph flow whose from/to mix agents + functions passes reference
    validation; Phase 7 then applies the STRUCTURAL graph checks (this
    fixture graph never reaches END, so it fails on that — not on refs). A
    dangling reference still fails on the reference itself."""
    project = _copy_project(tmp_path, "memory_graph")
    system_yaml = project / "system.yaml"
    original = system_yaml.read_text()

    system_yaml.write_text(original.replace(_SEQ_FLOW, _GRAPH_FLOW_OK))
    with pytest.raises(CompileError) as valid_refs:
        compile_project(project)
    assert "no path to END" in str(valid_refs.value)    # structural check
    assert "unknown node" not in str(valid_refs.value)  # refs resolved fine

    system_yaml.write_text(original.replace(_SEQ_FLOW, _GRAPH_FLOW_DANGLING))
    with pytest.raises(CompileError) as dangling:
        compile_project(project)
    assert "unknown node 'ghost_node'" in str(dangling.value)
    assert "/flow/edges/1/to" in str(dangling.value)


_SEMANTIC_CACHE_YAML = """
semantic_cache:
  embedder_binding:
    provider: voyage
    model: voyage-3
  similarity_threshold: 0.95
  ttl_s: 3600
  scope: agent
  backend: in_process
"""


@pytest.mark.integration
def test_memory_plus_semantic_cache_warns_about_bypass(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent configuring BOTH memory and semantic_cache gets the cache
    bypassed at runtime (2c deviation 4); the bypass must be VISIBLE — a
    compile warning on stderr + a WarningEvent in the audit trail."""
    monkeypatch.setenv("VOYAGE_API_KEY", "fake-voyage-key-for-tests")
    project = _copy_project(tmp_path, "memory_cached")
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    agent_yaml.write_text(agent_yaml.read_text() + _SEMANTIC_CACHE_YAML)

    transport = MemoryTransport()
    # MemoryTransport asserts every request hits api.anthropic.com — a
    # consulted semantic cache would embed via voyage and fail the run, so
    # exit 0 also proves the bypass itself.
    assert execute_run(project, _turns(2), transport=transport.build()) == 0
    err = capsys.readouterr().err
    assert "cache.semantic.bypassed_by_memory" in err
    assert "'hello_agent'" in err

    warnings = _by_event(_events(_run_dirs(tmp_path)[-1]), "warning")
    bypass = [w for w in warnings
              if w["category"] == "cache.semantic.bypassed_by_memory"]
    assert len(bypass) == 1
    assert bypass[0]["agent_name"] == "hello_agent"


# --- artifact hygiene ---------------------------------------------------------------------


@pytest.mark.integration
def test_run_id_threaded_and_no_secrets_in_artifact(tmp_path: Path) -> None:
    transport = MemoryTransport()
    assert execute_run(MEMORY_DIR, _turns(4), transport=transport.build()) == 0
    run_dir = _run_dirs(tmp_path)[-1]
    events = _events(run_dir)
    assert {e["run_id"] for e in events} == {run_dir.name}
    kinds = {e["event"] for e in events}
    assert {"memory.read", "memory.write", "memory.consolidate",
            "function_node.started", "function_node.completed"} <= kinds
    combined = "".join(p.read_text() for p in run_dir.iterdir() if p.is_file())
    assert "fake-anthropic-key-for-tests" not in combined
