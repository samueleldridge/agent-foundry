"""Phase 3 exit-gate integration tests (docs/03 § Phase 3).

Same posture as every prior suite: no live API keys — the full real path
(compile → StateGraph → checkpointer → resume → spans → streaming) runs
with only the HTTP layer replaced by httpx.MockTransport.

The kill-mid-run gate is simulated per the docs/03 recipe: the fake
provider serves the tool round, the tool executes (its result is
checkpointed at the tools-node boundary), then the NEXT provider call
returns 401 → the run dies exactly like a killed process (state on disk,
process gone). A second execute_run with the SAME run id and a healthy
transport must resume from the checkpoint and complete WITHOUT re-running
the tool or the planning round.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from foundry.cli.run import execute_run
from foundry.core import RunId

REPO_ROOT = Path(__file__).resolve().parents[2]
HELLO_DIR = REPO_ROOT / "projects" / "hello"
MEMORY_DIR = REPO_ROOT / "projects" / "memory_hello"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


def _tool_use_turn(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": "Checking."}, *blocks],
        "stop_reason": "tool_use",
        "model": "claude-haiku-4-5",
        "usage": {"input_tokens": 50, "output_tokens": 30},
    }


def _final_turn(greeting: str) -> dict[str, Any]:
    return {
        "content": [{"type": "text", "text": json.dumps({"greeting": greeting})}],
        "stop_reason": "end_turn",
        "model": "claude-haiku-4-5",
        "usage": {"input_tokens": 90, "output_tokens": 25},
    }


def _get_time_block(block_id: str = "tu_1") -> dict[str, Any]:
    return {
        "type": "tool_use",
        "id": block_id,
        "name": "get_time",
        "input": {"path": "/api/timezone/Etc/UTC"},
    }


class KillableTransport:
    """Scripted anthropic + time-service fake that can 401 chosen LLM calls
    (the docs/03 'raise after N tool calls' kill simulation)."""

    def __init__(
        self,
        llm_turns: list[dict[str, Any]],
        *,
        kill_on_llm_calls: set[int] | None = None,
    ) -> None:
        self.llm_turns = llm_turns
        self.kill_on_llm_calls = kill_on_llm_calls or set()
        self.llm_requests: list[dict[str, Any]] = []
        self.time_requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.anthropic.com":
            self.llm_requests.append(json.loads(request.content))
            call_number = len(self.llm_requests)
            if call_number in self.kill_on_llm_calls:
                return httpx.Response(
                    401, json={"error": {"message": "simulated process kill"}}
                )
            return httpx.Response(200, json=self.llm_turns[call_number - 1])
        if request.url.host == "worldtimeapi.org":
            self.time_requests.append(request)
            return httpx.Response(
                200, json={"datetime": "2026-07-09T10:00:00+00:00"}
            )
        raise AssertionError(f"unexpected host: {request.url.host}")

    def build(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _run_dir(tmp_path: Path, run_id: str) -> Path:
    return tmp_path / "foundry_home" / "runs" / run_id


def _events(run_dir: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]


# --- exit gate: kill mid-run + resume with the same run id ---------------------------


@pytest.mark.integration
def test_kill_after_tool_call_then_resume_completes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    run_id = str(RunId.new())
    turns = [
        _tool_use_turn(_get_time_block()),
        _final_turn("Hello, world! 10:00 UTC."),
    ]

    # Process 1: plan → tool executes (checkpointed) → next LLM call dies.
    process1 = KillableTransport(turns, kill_on_llm_calls={2})
    code = execute_run(
        HELLO_DIR,
        '{"name": "world"}',
        transport=process1.build(),
        checkpoint="sqlite",
        run_id=run_id,
    )
    assert code == 1
    assert len(process1.time_requests) == 1  # the tool ran before the kill
    err = capsys.readouterr().err
    assert "ProviderAuthError" in err

    # The dev checkpoint db exists and is inspectable (manual-test surface).
    db = tmp_path / "foundry_home" / "checkpoints" / "hello.sqlite"
    assert db.exists()

    # Process 2: same run id, healthy transport → resumes and completes.
    # It serves ONLY the final turn: the resumed run continues at the
    # post-tool LLM round, so the planning round must never be re-asked.
    process2 = KillableTransport([_final_turn("Hello, world! 10:00 UTC.")])
    code = execute_run(
        HELLO_DIR,
        '{"name": "world"}',
        transport=process2.build(),
        checkpoint="sqlite",
        run_id=run_id,
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"greeting": "Hello, world! 10:00 UTC."}

    # Resumed EXACTLY where it died: the first request process 2 sends is
    # the post-tool round — its conversation already carries the tool
    # result restored from the checkpoint...
    assert len(process2.llm_requests) == 1
    resumed_messages = process2.llm_requests[0]["messages"]
    tool_result = resumed_messages[-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert "2026-07-09T10:00:00" in json.dumps(tool_result["content"])
    # ...and the tool did NOT re-execute in process 2.
    assert len(process2.time_requests) == 0

    # One artifact dir for the whole run: events append across processes
    # with a continuous sequence; the step events appear exactly once.
    events = _events(_run_dir(tmp_path, run_id))
    assert [e["sequence"] for e in events] == list(range(len(events)))
    assert {e["run_id"] for e in events} == {run_id}
    names = [e["event"] for e in events]
    assert names.count("run.started") == 2  # one per process
    assert names.count("run.failed") == 1
    assert names.count("run.completed") == 1
    assert names.count("agent.started") == 1  # begin node not re-run
    assert names.count("tool.completed") == 1
    assert names.count("agent.completed") == 1

    metadata = json.loads(
        (_run_dir(tmp_path, run_id) / "metadata.json").read_text()
    )
    assert metadata["status"] == "completed"
    assert metadata["resumed"] is True
    assert metadata["checkpointer"] == "sqlite"


@pytest.mark.integration
def test_memory_turn_loop_resumes_mid_conversation(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Kill during turn 2 of a 3-turn memory run → resume completes the
    remaining turns; turn 1 is not re-asked; the checkpointed conv
    (FoundryMessages, recent window, turn counters) round-trips serde."""
    run_id = str(RunId.new())

    def agent_reply(n: int) -> dict[str, Any]:
        return {
            "content": [
                {"type": "text", "text": json.dumps({"reply": f"ack-{n}"})}
            ],
            "stop_reason": "end_turn",
            "model": "claude-haiku-4-5",
            "usage": {"input_tokens": 120, "output_tokens": 25},
        }

    def consolidation() -> dict[str, Any]:
        return {
            "content": [{"type": "text", "text": "- FACTS: three turns seen"}],
            "stop_reason": "end_turn",
            "model": "claude-haiku-4-5",
            "usage": {"input_tokens": 120, "output_tokens": 25},
        }

    input_json = json.dumps(
        {"raw_turns": ["  turn-01  ", "  turn-02  ", "  turn-03  "]}
    )

    # Process 1: turn 1 completes (checkpointed), turn 2's LLM call dies.
    process1 = KillableTransport([agent_reply(1)], kill_on_llm_calls={2})
    code = execute_run(
        MEMORY_DIR, input_json, transport=process1.build(),
        checkpoint="sqlite", run_id=run_id,
    )
    assert code == 1
    assert len(process1.llm_requests) == 2  # turn 1 ok, turn 2 killed
    capsys.readouterr()

    # Process 2: serves turn 2, turn 3, then the every-3-turns consolidation.
    process2 = KillableTransport(
        [agent_reply(2), agent_reply(3), consolidation()]
    )
    code = execute_run(
        MEMORY_DIR, input_json, transport=process2.build(),
        checkpoint="sqlite", run_id=run_id,
    )
    assert code == 0
    printed = capsys.readouterr().out
    assert json.loads(printed)["reply"] == "ack-3"

    # Turn 1 was NOT re-asked; process 2 starts at turn 2, and its request
    # already carries turn 1 in the restored working-memory window.
    first_resumed = process2.llm_requests[0]["messages"]
    assert "turn-02" in first_resumed[-1]["content"][0]["text"]
    window_texts = json.dumps(first_resumed)
    assert "turn-01" in window_texts  # restored from the checkpointed state
    assert "ack-1" in window_texts

    # Full-run audit: 3 agent turns + 1 consolidation, no duplicates.
    events = _events(_run_dir(tmp_path, run_id))
    names = [e["event"] for e in events]
    assert names.count("memory.consolidate") == 1
    assert names.count("agent.started") == 1
    assert names.count("agent.completed") == 1
    assert [e["sequence"] for e in events] == list(range(len(events)))
    final_state = json.loads(
        (_run_dir(tmp_path, run_id) / "final_state.json").read_text()
    )["state"]
    assert len(final_state["messages"]) == 6  # 3 turns x (user + assistant)
    assert final_state["reply"] == "ack-3"


# --- exit gate: --stream emits incremental output -------------------------------------


@pytest.mark.integration
def test_stream_emits_runevents_as_jsonl_then_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = KillableTransport(
        [_tool_use_turn(_get_time_block()), _final_turn("Hi! 10:00 UTC.")]
    )
    code = execute_run(
        HELLO_DIR, '{"name": "world"}', transport=transport.build(), stream=True
    )
    assert code == 0
    out = capsys.readouterr().out
    lines = out.splitlines()
    event_lines = [json.loads(line) for line in lines if '"event"' in line]
    names = [e["event"] for e in event_lines]
    # The full lifecycle streams incrementally, in order, run_id-threaded.
    assert names[0] == "run.started"
    assert names[-1] == "run.completed"
    for expected in ("agent.started", "llm.started", "tool.started",
                     "tool.completed", "llm.completed", "agent.completed"):
        assert expected in names
    assert names.index("llm.started") < names.index("tool.started")
    assert len({e["run_id"] for e in event_lines}) == 1
    # The typed output still prints (after the event stream).
    assert json.loads(out[out.rindex("{"):])  # last JSON object parses
    assert '"greeting"' in out


# --- exit gate: trace spans carry run id / system name / agent name -------------------


@pytest.mark.integration
def test_spans_include_run_id_system_and_agent(
    tmp_path: Path, span_exporter: Any, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = KillableTransport(
        [_tool_use_turn(_get_time_block()), _final_turn("Hello!")]
    )
    assert execute_run(
        HELLO_DIR, '{"name": "world"}', transport=transport.build()
    ) == 0
    run_id = json.loads(
        (sorted((tmp_path / "foundry_home" / "runs").iterdir())[-1]
         / "metadata.json").read_text()
    )["run_id"]

    spans = span_exporter.get_finished_spans()
    by_name: dict[str, list[Any]] = {}
    for span in spans:
        by_name.setdefault(span.name, []).append(dict(span.attributes or {}))

    (run_attrs,) = by_name["foundry.run"]
    assert run_attrs["run_id"] == run_id
    assert run_attrs["project"] == "hello"
    assert run_attrs["status"] == "success"
    assert "system_version" in run_attrs and "pin_set_hash" in run_attrs

    node_attrs = by_name["foundry.node"]
    node_names = {a["node"] for a in node_attrs}
    assert {"hello_agent", "hello_agent__llm", "hello_agent__tools",
            "hello_agent__finish"} <= node_names
    assert all(a["run_id"] == run_id for a in node_attrs)
    assert all(a["project"] == "hello" for a in node_attrs)
    assert all(a["agent"] == "hello_agent" for a in node_attrs)

    llm_attrs = by_name["foundry.llm"]
    assert len(llm_attrs) == 2  # tool round + final round
    for attrs in llm_attrs:
        assert attrs["run_id"] == run_id
        assert attrs["agent"] == "hello_agent"
        assert attrs["provider"] == "anthropic"
        assert attrs["prompt_tokens"] > 0
        assert "stop_reason" in attrs

    # node spans parent under the run span (one trace per run)
    trace_ids = {s.context.trace_id for s in spans}
    assert len(trace_ids) == 1


# --- checkpointer plumbing surfaces ---------------------------------------------------


@pytest.mark.integration
def test_checkpoint_none_still_runs_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = KillableTransport([_final_turn("Hello, world!")])
    code = execute_run(
        HELLO_DIR, '{"name": "world"}', transport=transport.build(),
        checkpoint="none",
    )
    assert code == 0
    assert json.loads(capsys.readouterr().out) == {"greeting": "Hello, world!"}
    assert not (tmp_path / "foundry_home" / "checkpoints").exists()


@pytest.mark.integration
def test_invalid_checkpoint_and_run_id_are_exit_2(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert execute_run(HELLO_DIR, "{}", checkpoint="postgres") == 2
    assert "--checkpoint" in capsys.readouterr().err
    assert execute_run(HELLO_DIR, "{}", run_id="not-a-ulid") == 2
    assert "--run-id" in capsys.readouterr().err


@pytest.mark.integration
def test_rerun_of_a_completed_run_id_starts_fresh(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A completed thread has no pending nodes: rerunning the same id is a
    fresh invocation (documented Phase 3 semantics), not a stale replay."""
    run_id = str(RunId.new())
    for greeting in ("Hello, one!", "Hello, two!"):
        transport = KillableTransport([_final_turn(greeting)])
        code = execute_run(
            HELLO_DIR, '{"name": "world"}', transport=transport.build(),
            checkpoint="sqlite", run_id=run_id,
        )
        assert code == 0
    out = capsys.readouterr().out
    assert "Hello, two!" in out
    metadata = json.loads(
        (_run_dir(tmp_path, run_id) / "metadata.json").read_text()
    )
    assert metadata["resumed"] is False
