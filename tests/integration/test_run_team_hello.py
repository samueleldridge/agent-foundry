"""Phase 7 exit-gate integration tests against projects/team_hello.

Supervisor + 2 workers end-to-end, typed handoff tools, structural state
visibility, and the full HITL loop: pause on ApprovalRequired →
`foundry approvals list` → `foundry resume --approve/--reject` → final
output reflects the decision. Kill + resume still works mid-flow.

Live-key runs are the operator's manual step
(docs/_manual_tests/phase_7.md); here the full real path — compile,
handoff tool synthesis, routers, checkpointer, interrupt — runs against
httpx.MockTransport with only the HTTP layer substituted (the established
Phase 1 pattern). The transport routes scripted turns PER AGENT by
matching each request's system prompt, so the supervisor loop's call
order is exercised, not assumed.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from foundry.cli.resume import execute_approvals_list, execute_resume
from foundry.cli.run import execute_run
from foundry.core import RunId

REPO_ROOT = Path(__file__).resolve().parents[2]
TEAM_DIR = REPO_ROOT / "projects" / "team_hello"

RUN_INPUT = json.dumps(
    {"request": "the new release shipping", "audience": "the team"}
)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


def _copy_team(tmp_path: Path) -> Path:
    project = tmp_path / "team_hello"
    shutil.copytree(TEAM_DIR, project)
    return project


def _turn(*blocks: dict[str, Any], stop: str = "end_turn") -> dict[str, Any]:
    return {
        "content": list(blocks),
        "stop_reason": stop,
        "model": "claude-haiku-4-5",
        "usage": {"input_tokens": 60, "output_tokens": 30},
    }


def _text(payload: dict[str, Any]) -> dict[str, Any]:
    return {"type": "text", "text": json.dumps(payload)}


def _tool_use(name: str, inputs: dict[str, Any], block_id: str) -> dict[str, Any]:
    return {"type": "tool_use", "id": block_id, "name": name, "input": inputs}


_AGENT_MARKERS = {
    "coordinator": "coordinator — system prompt",
    "drafter": "drafter — system prompt",
    "publisher": "publisher — system prompt",
}


class TeamTransport:
    """Scripted per-agent turns, routed by the request's system prompt."""

    def __init__(self, turns: dict[str, list[dict[str, Any]]]) -> None:
        self.turns = {agent: list(queue) for agent, queue in turns.items()}
        self.requests: list[tuple[str, dict[str, Any]]] = []
        self.fail_at_call: int | None = None
        self._calls = 0

    def agent_of(self, body: dict[str, Any]) -> str:
        system = body.get("system", "")
        for agent, marker in _AGENT_MARKERS.items():
            if marker in system:
                return agent
        raise AssertionError(f"unrecognised system prompt: {system[:120]}")

    def handler(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.anthropic.com"
        body = json.loads(request.content)
        agent = self.agent_of(body)
        self.requests.append((agent, body))
        self._calls += 1
        if self.fail_at_call is not None and self._calls == self.fail_at_call:
            self.fail_at_call = None  # fail once, then recover
            return httpx.Response(401, json={"error": {"message": "nope"}})
        queue = self.turns[agent]
        assert queue, f"no scripted turn left for {agent}"
        return httpx.Response(200, json=queue.pop(0))

    def build(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _happy_turns() -> dict[str, list[dict[str, Any]]]:
    return {
        "coordinator": [
            _turn(
                _tool_use(
                    "transfer_to_drafter",
                    {"reason": "no draft exists yet; drafter goes first"},
                    "tu_c1",
                ),
                stop="tool_use",
            ),
            _turn(
                _tool_use(
                    "transfer_to_publisher",
                    {"reason": "draft ready; publish it to the channel"},
                    "tu_c2",
                ),
                stop="tool_use",
            ),
            _turn(
                _tool_use(
                    "transfer_to_end",
                    {"reason": "draft published; work is complete"},
                    "tu_c3",
                ),
                stop="tool_use",
            ),
            _turn(
                _text(
                    {
                        "final_summary": "Drafted and published the release "
                        "greeting (publish_status: published)."
                    }
                )
            ),
        ],
        "drafter": [
            _turn(_text({"draft": "Hello team - the new release just shipped!"})),
        ],
        "publisher": [
            _turn(
                _tool_use(
                    "publish_greeting",
                    {"text": "Hello team - the new release just shipped!"},
                    "tu_p1",
                ),
                stop="tool_use",
            ),
            _turn(_text({"publish_status": "published"})),
        ],
    }


def _events(tmp_path: Path, run_id: str) -> list[dict[str, Any]]:
    path = tmp_path / "foundry_home" / "runs" / run_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _metadata(tmp_path: Path, run_id: str) -> dict[str, Any]:
    path = tmp_path / "foundry_home" / "runs" / run_id / "metadata.json"
    return json.loads(path.read_text())


# --- hero: supervisor + 2 workers + HITL approve --------------------------------


@pytest.mark.integration
def test_supervisor_hitl_pause_then_approve_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit gates: supervisor+2 workers runs end-to-end; approval required
    mid-run pauses the process; CLI shows pending; resume --approve
    continues; the final output reflects the approval; handoff events
    complete; workers only ever see their read scope."""
    project = _copy_team(tmp_path)
    transport = TeamTransport(_happy_turns())
    run_id = str(RunId.new())

    code = execute_run(
        project,
        RUN_INPUT,
        transport=transport.build(),
        checkpoint="sqlite",
        run_id=run_id,
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "run paused: approval required" in err
    assert "publish-" in err  # the stable approval id
    assert f"foundry resume {run_id} --approve" in err

    # The pause is durable + discoverable.
    metadata = _metadata(tmp_path, run_id)
    assert metadata["status"] == "approval_pending"
    pending = metadata["pending_approval"]
    assert pending["agent_name"] == "publisher"
    assert "Publish this greeting" in pending["prompt"]
    assert execute_approvals_list(None) == 0
    listing = capsys.readouterr().out
    assert run_id in listing
    assert "team_hello" in listing

    # Events so far: handoffs coordinator→drafter→coordinator→publisher,
    # then approval.required — and NO run.completed success yet.
    events = _events(tmp_path, run_id)
    handoffs = [
        (e["from_agent"], e["to_agent"], e["trigger"])
        for e in events
        if e["event"] == "handoff"
    ]
    assert handoffs == [
        ("coordinator", "drafter", "llm"),
        ("drafter", "coordinator", "rule"),
        ("coordinator", "publisher", "llm"),
    ]
    approval_events = [e for e in events if e["event"] == "approval.required"]
    assert len(approval_events) == 1
    assert approval_events[0]["agent_name"] == "publisher"
    paused = [e for e in events if e["event"] == "run.completed"]
    assert [e["status"] for e in paused] == ["approval_pending"]

    # Resume with approval — the run completes.
    code = execute_resume(run_id, approve=True, transport=transport.build())
    assert code == 0
    out = capsys.readouterr().out
    output = json.loads(out)
    assert "published" in output["final_summary"]

    metadata = _metadata(tmp_path, run_id)
    assert metadata["status"] == "completed"
    final_state = json.loads(
        (tmp_path / "foundry_home" / "runs" / run_id / "final_state.json")
        .read_text()
    )["state"]
    assert final_state["publish_status"] == "published"
    assert final_state["draft"].startswith("Hello team")

    # Audit completeness: approval.resolved once, sequence continued (the
    # resumed events extend the same file with increasing sequence).
    events = _events(tmp_path, run_id)
    resolved = [e for e in events if e["event"] == "approval.resolved"]
    assert len(resolved) == 1
    assert resolved[0]["decision"] == "approved"
    sequences = [e["sequence"] for e in events]
    assert sequences == sorted(sequences)
    assert len(set(sequences)) == len(sequences)
    handoffs = [
        (e["from_agent"], e["to_agent"]) for e in events
        if e["event"] == "handoff"
    ]
    assert handoffs[-2:] == [
        ("publisher", "coordinator"),
        ("coordinator", "END"),
    ]
    # Exactly one terminal success event.
    completed = [
        e for e in events
        if e["event"] == "run.completed" and e["status"] == "success"
    ]
    assert len(completed) == 1

    # STRUCTURAL visibility (exit gate): the publisher's prompts only ever
    # carried its read scope ({draft}) — never request/audience; the
    # drafter never saw publish_status/final_summary.
    for agent, body in transport.requests:
        user_texts = [
            block["text"]
            for message in body["messages"]
            if message["role"] == "user"
            for block in message["content"]
            if block.get("type") == "text"
        ]
        first_input = json.loads(user_texts[0])
        if agent == "publisher":
            assert set(first_input) == {"draft"}
            assert "release shipping" not in json.dumps(user_texts)
        if agent == "drafter":
            assert set(first_input) == {"request", "audience"}


@pytest.mark.integration
def test_reject_feeds_reason_back_and_run_completes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--reject --reason: the tool's re-invocation sees the rejection, the
    publisher reports it, and the coordinator's summary reflects it."""
    project = _copy_team(tmp_path)
    turns = _happy_turns()
    turns["publisher"][1] = _turn(
        _text({"publish_status": "rejected: operator rejected: tone is off"})
    )
    turns["coordinator"][3] = _turn(
        _text(
            {
                "final_summary": "Draft written but publication was "
                "rejected: operator rejected: tone is off."
            }
        )
    )
    transport = TeamTransport(turns)
    run_id = str(RunId.new())

    assert (
        execute_run(
            project,
            RUN_INPUT,
            transport=transport.build(),
            checkpoint="sqlite",
            run_id=run_id,
        )
        == 0
    )
    capsys.readouterr()
    code = execute_resume(
        run_id, reject=True, reason="tone is off", transport=transport.build()
    )
    assert code == 0
    output = json.loads(capsys.readouterr().out)
    assert "rejected" in output["final_summary"]

    # The rejected tool result carried the operator's reason to the agent:
    # the publisher's post-resume request contains the tool_result text.
    publisher_bodies = [
        body for agent, body in transport.requests if agent == "publisher"
    ]
    tool_results = json.dumps(publisher_bodies[-1]["messages"])
    assert "operator rejected: tone is off" in tool_results
    assert '\\"published\\":false' in tool_results

    events = _events(tmp_path, run_id)
    resolved = [e for e in events if e["event"] == "approval.resolved"]
    assert resolved[0]["decision"] == "rejected"
    assert resolved[0]["reason"] == "tone is off"

    final_state = json.loads(
        (tmp_path / "foundry_home" / "runs" / run_id / "final_state.json")
        .read_text()
    )["state"]
    assert final_state["publish_status"].startswith("rejected")


@pytest.mark.integration
def test_resume_without_decision_shows_pending(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_team(tmp_path)
    transport = TeamTransport(_happy_turns())
    run_id = str(RunId.new())
    assert (
        execute_run(
            project,
            RUN_INPUT,
            transport=transport.build(),
            checkpoint="sqlite",
            run_id=run_id,
        )
        == 0
    )
    capsys.readouterr()
    assert execute_resume(run_id) == 0
    out = capsys.readouterr().out
    assert "approval pending" in out
    assert "Publish this greeting" in out
    assert "--approve" in out


@pytest.mark.integration
def test_kill_and_resume_multi_agent_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit gate: kill + resume still works in multi-agent runs. The
    coordinator's second turn 401s (the 'kill'); rerunning the same run id
    resumes from the checkpoint — the drafter is NOT re-invoked — and the
    run then pauses at the approval as normal."""
    project = _copy_team(tmp_path)
    transport = TeamTransport(_happy_turns())
    transport.fail_at_call = 3  # coordinator turn 2 (after drafter's draft)
    run_id = str(RunId.new())

    code = execute_run(
        project,
        RUN_INPUT,
        transport=transport.build(),
        checkpoint="sqlite",
        run_id=run_id,
    )
    assert code == 1  # the "kill"
    drafter_calls = sum(1 for a, _ in transport.requests if a == "drafter")
    assert drafter_calls == 1
    capsys.readouterr()

    code = execute_run(
        project,
        RUN_INPUT,
        transport=transport.build(),
        checkpoint="sqlite",
        run_id=run_id,
    )
    assert code == 0
    err = capsys.readouterr().err
    assert "run paused: approval required" in err
    # Resume did NOT re-run the drafter (its turn was checkpointed).
    drafter_calls = sum(1 for a, _ in transport.requests if a == "drafter")
    assert drafter_calls == 1
    metadata = _metadata(tmp_path, run_id)
    assert metadata["status"] == "approval_pending"

    # ...and the approval path still works after the kill+resume.
    assert (
        execute_resume(run_id, approve=True, transport=transport.build()) == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert "published" in output["final_summary"]
