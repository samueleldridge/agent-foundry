"""Phase 7 exit-gate integration tests: parallel fan-out/fan-in under REAL
concurrency, graph conditional routing, nested flows (supervisor holding a
parallel group), and max_hops / max_iterations guardrails.

Fixtures are built programmatically in tmp_path (the parallel/graph shapes
need bespoke state schemas); the guardrail tests reuse projects/team_hello
with a patched termination block. LLM HTTP runs against
httpx.MockTransport (established pattern); the parallel-branch functions
rendezvous on an asyncio.Barrier so a sequential regression DEADLOCKS
(bounded by a timeout) instead of flaking on machine load.
"""

from __future__ import annotations

import json
import shutil
import textwrap
from pathlib import Path
from typing import Any

import httpx
import pytest

from foundry.cli.run import execute_run
from foundry.core import RunId

REPO_ROOT = Path(__file__).resolve().parents[2]
TEAM_DIR = REPO_ROOT / "projects" / "team_hello"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


# --- fixture builders -----------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content))


def _write_agent(
    project: Path,
    name: str,
    *,
    read: list[str],
    write: list[str],
    output_fields: list[str],
    marker: str,
) -> None:
    agent_dir = project / "agents" / name
    _write(
        agent_dir / "agent.yaml",
        f"""\
        name: {name}
        description: test agent {name}
        model_binding:
          provider: openai
          model: gpt-5-mini
          settings: {{max_tokens: 256, temperature: 0.0}}
        prompt: {{version: v1, path: prompts/v1.md}}
        output: {{schema: output_schema.py::Output}}
        tools: []
        iteration_limit: 4
        state_visibility:
          read: [{", ".join(read)}]
          write: [{", ".join(write)}]
        """,
    )
    _write(agent_dir / "prompts" / "v1.md", f"# {marker}\n\nAnswer as JSON.\n")
    fields = "\n".join(f"    {f}: str" for f in output_fields)
    _write(
        agent_dir / "output_schema.py",
        f"from pydantic import BaseModel\n\n\nclass Output(BaseModel):\n{fields}\n",
    )


def _write_function(
    project: Path,
    name: str,
    *,
    read: list[str],
    write: list[str],
    body: str,
) -> None:
    function_dir = project / "functions" / name
    _write(
        function_dir / "function.yaml",
        f"""\
        name: {name}
        description: test function {name}
        function: function.py::run
        state_visibility:
          read: [{", ".join(read)}]
          write: [{", ".join(write)}]
        timeout_s: 15.0
        """,
    )
    _write(function_dir / "function.py", body)


def _events(tmp_path: Path, run_id: str) -> list[dict[str, Any]]:
    path = tmp_path / "foundry_home" / "runs" / run_id / "events.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def _final_state(tmp_path: Path, run_id: str) -> dict[str, Any]:
    path = tmp_path / "foundry_home" / "runs" / run_id / "final_state.json"
    return json.loads(path.read_text())["state"]


def _turn(text: str) -> dict[str, Any]:
    return {
        "model": "gpt-5-mini",
        "choices": [
            {
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 20},
    }


def _tool_turn(name: str, inputs: dict[str, Any], block_id: str) -> dict[str, Any]:
    return {
        "model": "gpt-5-mini",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": block_id,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(inputs),
                            },
                        }
                    ],
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 40, "completion_tokens": 20},
    }


def _system_text(body: dict[str, Any]) -> str:
    """openai wire shape: the system prompt is a role=system message."""
    return next(
        (m["content"] for m in body.get("messages", [])
         if m.get("role") == "system"),
        "",
    )


class MarkerTransport:
    """Scripted turns routed by a marker found in the system prompt."""

    def __init__(self, turns: dict[str, list[dict[str, Any]]]) -> None:
        self.turns = {marker: list(queue) for marker, queue in turns.items()}
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        system = _system_text(body)
        for marker, queue in self.turns.items():
            if marker in system:
                self.requests.append((marker, body))
                assert queue, f"no scripted turn left for {marker}"
                return httpx.Response(200, json=queue.pop(0))
        raise AssertionError(f"unrecognised system prompt: {system[:120]}")

    def build(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


# --- parallel fan-out / fan-in under real concurrency -------------------------------


_BRANCH_TEMPLATE = '''\
"""Parallel branch {label}: rendezvous with the sibling branches, then
write to every reducer kind. The barrier PROVES real concurrency: if the
branches ran sequentially, the first would block on the barrier until the
timeout and the run would fail."""

import asyncio
import sys
from typing import Any


async def run(state_view: dict[str, Any], ctx: Any) -> dict[str, Any]:
    shared = sys.modules["asyncio"].__dict__
    barrier = shared.setdefault("_foundry_test_barrier_{key}", asyncio.Barrier(3))
    await asyncio.wait_for(barrier.wait(), timeout=15)
    return {{
        "notes": ["{label}"],
        "results": {{"{label}": "value-{label}"}},
        "last": "{label}",
        "first_only": {first_only},
    }}
'''


def _build_fanout_project(tmp_path: Path, key: str) -> Path:
    project = tmp_path / "fanout"
    _write(
        project / "system.yaml",
        """\
        name: fanout
        description: parallel fan-out/fan-in reducer fixture
        agents: [reporter]
        functions: [branch_a, branch_b, branch_c, aggregate]
        state: state.yaml
        flow:
          type: parallel
          parallel_branches: [branch_a, branch_b, branch_c]
          join: aggregate
          then: [reporter]
        guardrails: {max_iterations: 5, max_hops: 10}
        """,
    )
    _write(
        project / "state.yaml",
        """\
        schema:
          seed: {type: str, description: input}
          notes: {type: "list[str]", description: appended by every branch}
          results: {type: "dict[str, str]", description: merged per branch}
          last: {type: "str | None", description: serialisation-order winner}
          first_only: {type: "str | None", description: replace_if_set survivor}
          summary: {type: "str | None", description: join product}
          report: {type: "str | None", description: reporter output}
        reducers:
          notes: append
          results: merge
          last: last_write_wins
          first_only: replace_if_set
        visibility:
          branch_a: {read: [seed], write: [notes, results, last, first_only]}
          branch_b: {read: [seed], write: [notes, results, last, first_only]}
          branch_c: {read: [seed], write: [notes, results, last, first_only]}
          aggregate:
            read: [notes, results, last, first_only]
            write: [summary]
          reporter: {read: [summary], write: [report]}
        """,
    )
    for label in ("a", "b", "c"):
        first_only = '"set-by-a"' if label == "a" else "None"
        _write_function(
            project,
            f"branch_{label}",
            read=["seed"],
            write=["notes", "results", "last", "first_only"],
            body=_BRANCH_TEMPLATE.format(
                label=label, first_only=first_only, key=key
            ),
        )
    _write_function(
        project,
        "aggregate",
        read=["notes", "results", "last", "first_only"],
        write=["summary"],
        body=textwrap.dedent(
            '''\
            from typing import Any


            async def run(state_view: dict[str, Any], ctx: Any) -> dict[str, Any]:
                summary = (
                    f"notes={sorted(state_view['notes'])}"
                    f"|results={sorted(state_view['results'])}"
                    f"|first_only={state_view['first_only']}"
                )
                return {"summary": summary}
            '''
        ),
    )
    _write_agent(
        project,
        "reporter",
        read=["summary"],
        write=["report"],
        output_fields=["report"],
        marker="reporter-agent-prompt",
    )
    return project


@pytest.mark.integration
def test_parallel_fanout_fanin_reducers_under_real_concurrency(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Exit gate: 3 branches run CONCURRENTLY (barrier rendezvous), their
    writes merge via append / merge / lww / replace_if_set, the join sees
    the merged state, and the sequential `then` step completes."""
    project = _build_fanout_project(tmp_path, key="fanout1")
    transport = MarkerTransport(
        {"reporter-agent-prompt": [_turn('{"report": "all branches in"}')]}
    )
    run_id = str(RunId.new())
    code = execute_run(
        project,
        '{"seed": "go"}',
        transport=transport.build(),
        checkpoint="memory",
        run_id=run_id,
    )
    assert code == 0

    state = _final_state(tmp_path, run_id)
    # APPEND: all three branches' notes survive (order = completion order).
    assert sorted(state["notes"]) == ["a", "b", "c"]
    # MERGE: namespaced dict writes union cleanly.
    assert state["results"] == {
        "a": "value-a", "b": "value-b", "c": "value-c",
    }
    # LAST_WRITE_WINS: a serialisation-order winner — exactly one of them.
    assert state["last"] in {"a", "b", "c"}
    # REPLACE_IF_SET: branch a's value survives the None writes of b/c.
    assert state["first_only"] == "set-by-a"
    # The join consumed the MERGED state.
    assert state["summary"] == (
        "notes=['a', 'b', 'c']|results=['a', 'b', 'c']|first_only=set-by-a"
    )
    assert state["report"] == "all branches in"

    # Function events: all three branches + join + reporter ran.
    events = _events(tmp_path, run_id)
    started = [
        e["node_name"] for e in events if e["event"] == "function_node.started"
    ]
    assert sorted(started) == ["aggregate", "branch_a", "branch_b", "branch_c"]


# --- graph pattern: conditional routing --------------------------------------------


def _build_routes_project(tmp_path: Path) -> Path:
    project = tmp_path / "routes"
    _write(
        project / "system.yaml",
        """\
        name: routes
        description: graph conditional-routing fixture
        agents: [triage]
        functions: [low_handler, high_handler]
        state: state.yaml
        flow:
          type: graph
          start: triage
          edges:
            - {from: triage, to: low_handler, when: "state.severity == 'low'"}
            - {from: triage, to: high_handler}
            - {from: low_handler, to: END}
            - {from: high_handler, to: END}
        guardrails: {max_iterations: 5, max_hops: 10}
        """,
    )
    _write(
        project / "state.yaml",
        """\
        schema:
          ticket: {type: str, description: input}
          severity: {type: "str | None", description: triage verdict}
          handled_by: {type: "str | None", description: which handler ran}
        visibility:
          triage: {read: [ticket], write: [severity]}
          low_handler: {read: [severity], write: [handled_by]}
          high_handler: {read: [severity], write: [handled_by]}
        """,
    )
    for name in ("low_handler", "high_handler"):
        _write_function(
            project,
            name,
            read=["severity"],
            write=["handled_by"],
            body=textwrap.dedent(
                f'''\
                from typing import Any


                async def run(state_view: dict[str, Any], ctx: Any) -> dict[str, Any]:
                    return {{"handled_by": "{name}"}}
                '''
            ),
        )
    _write_agent(
        project,
        "triage",
        read=["ticket"],
        write=["severity"],
        output_fields=["severity"],
        marker="triage-agent-prompt",
    )
    return project


@pytest.mark.integration
@pytest.mark.parametrize(
    ("verdict", "expected_handler"),
    [("low", "low_handler"), ("high", "high_handler")],
)
def test_graph_routes_by_predicate(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    verdict: str,
    expected_handler: str,
) -> None:
    """Exit gate (docs/30 integration 2): predicates evaluate against the
    agent-written state; the trace shows the exact route taken; the
    untaken branch never runs."""
    project = _build_routes_project(tmp_path)
    transport = MarkerTransport(
        {"triage-agent-prompt": [_turn(json.dumps({"severity": verdict}))]}
    )
    run_id = str(RunId.new())
    code = execute_run(
        project,
        '{"ticket": "T-1"}',
        transport=transport.build(),
        checkpoint="memory",
        run_id=run_id,
    )
    assert code == 0
    state = _final_state(tmp_path, run_id)
    assert state["handled_by"] == expected_handler

    events = _events(tmp_path, run_id)
    handoffs = [
        (e["from_agent"], e["to_agent"], e["trigger"])
        for e in events
        if e["event"] == "handoff"
    ]
    assert handoffs == [
        ("triage", expected_handler, "rule"),
        (expected_handler, "END", "end"),
    ]
    ran = [
        e["node_name"] for e in events if e["event"] == "function_node.started"
    ]
    assert ran == [expected_handler]  # the other branch never executed


# --- nesting: supervisor whose worker is a parallel sub-flow ------------------------


def _build_org_project(tmp_path: Path) -> Path:
    project = tmp_path / "org"
    _write(
        project / "system.yaml",
        """\
        name: org
        description: supervisor with a nested parallel worker (docs/30 nesting)
        agents: [coordinator]
        functions: [scan_a, scan_b, merge_findings]
        state: state.yaml
        flow:
          type: supervisor
          supervisor: coordinator
          workers:
            - investigation:
                type: parallel
                parallel_branches: [scan_a, scan_b]
                join: merge_findings
          termination: {max_hops: 8, on_max_hops: error}
        guardrails: {max_iterations: 8, max_hops: 12}
        """,
    )
    _write(
        project / "state.yaml",
        """\
        schema:
          topic: {type: str, description: input}
          findings: {type: "list[str]", description: appended by scanners}
          merged: {type: "str | None", description: join product}
          brief: {type: "str | None", description: coordinator output}
        reducers: {findings: append}
        visibility:
          coordinator: {read: [topic, findings, merged], write: [brief]}
          scan_a: {read: [topic], write: [findings]}
          scan_b: {read: [topic], write: [findings]}
          merge_findings: {read: [findings], write: [merged]}
        """,
    )
    for label in ("a", "b"):
        _write_function(
            project,
            f"scan_{label}",
            read=["topic"],
            write=["findings"],
            body=textwrap.dedent(
                f'''\
                from typing import Any


                async def run(state_view: dict[str, Any], ctx: Any) -> dict[str, Any]:
                    return {{"findings": ["finding-{label}"]}}
                '''
            ),
        )
    _write_function(
        project,
        "merge_findings",
        read=["findings"],
        write=["merged"],
        body=textwrap.dedent(
            '''\
            from typing import Any


            async def run(state_view: dict[str, Any], ctx: Any) -> dict[str, Any]:
                return {"merged": "+".join(sorted(state_view["findings"]))}
            '''
        ),
    )
    _write_agent(
        project,
        "coordinator",
        read=["topic", "findings", "merged"],
        write=["brief"],
        output_fields=["brief"],
        marker="org-coordinator-prompt",
    )
    return project


@pytest.mark.integration
def test_supervisor_with_nested_parallel_worker_runs_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """docs/30 § Composition: the compiler handles nesting recursively —
    the supervisor's transfer_to_investigation tool routes into the
    parallel sub-flow, its branches fan out, the join merges, and control
    returns to the supervisor."""
    project = _build_org_project(tmp_path)
    transport = MarkerTransport(
        {
            "org-coordinator-prompt": [
                _tool_turn(
                    "transfer_to_investigation",
                    {"reason": "no findings yet; run the parallel scans"},
                    "tu_1",
                ),
                _tool_turn(
                    "transfer_to_end",
                    {"reason": "findings merged; time to brief"},
                    "tu_2",
                ),
                _turn(json.dumps({"brief": "two findings merged"})),
            ]
        }
    )
    run_id = str(RunId.new())
    code = execute_run(
        project,
        '{"topic": "release"}',
        transport=transport.build(),
        checkpoint="memory",
        run_id=run_id,
    )
    assert code == 0
    state = _final_state(tmp_path, run_id)
    assert sorted(state["findings"]) == ["finding-a", "finding-b"]
    assert state["merged"] == "finding-a+finding-b"
    assert state["brief"] == "two findings merged"

    events = _events(tmp_path, run_id)
    handoffs = [
        (e["from_agent"], e["to_agent"], e["trigger"])
        for e in events
        if e["event"] == "handoff"
    ]
    assert handoffs == [
        ("coordinator", "investigation", "llm"),
        ("investigation", "coordinator", "rule"),
        ("coordinator", "END", "end"),
    ]


# --- guardrails: max_hops (all three policies) + max_iterations ----------------------


def _copy_team_with_termination(tmp_path: Path, termination: str) -> Path:
    project = tmp_path / "team_hello"
    shutil.copytree(TEAM_DIR, project)
    system_yaml = project / "system.yaml"
    original = system_yaml.read_text()
    patched = original.replace(
        "  termination:\n    max_hops: 10\n    on_max_hops: error\n",
        termination,
    )
    assert patched != original, "termination block not found to patch"
    system_yaml.write_text(patched)
    return project


def _looping_turns(drafter_turns: int) -> dict[str, list[dict[str, Any]]]:
    """A coordinator that ALWAYS hands to the drafter — the contrived loop."""
    return {
        "coordinator — system prompt": [
            _tool_turn(
                "transfer_to_drafter",
                {"reason": f"iteration {i}: keep the loop spinning"},
                f"tu_loop_{i}",
            )
            for i in range(10)
        ],
        "drafter — system prompt": [
            _turn(json.dumps({"draft": f"draft {i}"}))
            for i in range(drafter_turns)
        ],
    }


TEAM_INPUT = json.dumps({"request": "loop", "audience": "the team"})


@pytest.mark.integration
def test_max_hops_error_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_team_with_termination(
        tmp_path, "  termination:\n    max_hops: 3\n    on_max_hops: error\n"
    )
    transport = MarkerTransport(_looping_turns(5))
    run_id = str(RunId.new())
    code = execute_run(
        project,
        TEAM_INPUT,
        transport=transport.build(),
        checkpoint="memory",
        run_id=run_id,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "MaxHopsExceededError" in err
    assert "max_hops" in err
    events = _events(tmp_path, run_id)
    failed = [e for e in events if e["event"] == "run.failed"]
    assert failed[-1]["error"]["error_class"] == "MaxHopsExceededError"


@pytest.mark.integration
def test_max_hops_return_partial_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_team_with_termination(
        tmp_path,
        "  termination:\n    max_hops: 3\n    on_max_hops: return_partial\n",
    )
    transport = MarkerTransport(_looping_turns(5))
    run_id = str(RunId.new())
    code = execute_run(
        project,
        TEAM_INPUT,
        transport=transport.build(),
        checkpoint="memory",
        run_id=run_id,
    )
    assert code == 0  # ends cleanly with the partial state
    events = _events(tmp_path, run_id)
    completed = [e for e in events if e["event"] == "run.completed"]
    assert completed[-1]["status"] == "max_hops"
    state = _final_state(tmp_path, run_id)
    assert state["draft"] is not None  # partial progress preserved


@pytest.mark.integration
def test_max_hops_escalate_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """escalate: one forced final handoff to the escalation worker, whose
    completion goes straight to END (no supervisor round-trip)."""
    project = _copy_team_with_termination(
        tmp_path,
        "  termination:\n    max_hops: 3\n    on_max_hops: escalate\n"
        "    escalate_to: drafter\n",
    )
    transport = MarkerTransport(_looping_turns(5))
    run_id = str(RunId.new())
    code = execute_run(
        project,
        TEAM_INPUT,
        transport=transport.build(),
        checkpoint="memory",
        run_id=run_id,
    )
    assert code == 0
    events = _events(tmp_path, run_id)
    handoffs = [
        (e["from_agent"], e["to_agent"], e["trigger"])
        for e in events
        if e["event"] == "handoff"
    ]
    # The final two transitions: the forced escalation dispatch (trigger
    # rule, not llm) and the escalation worker's terminal handoff to END.
    assert handoffs[-2] == ("coordinator", "drafter", "rule")
    assert handoffs[-1] == ("drafter", "END", "end")


@pytest.mark.integration
def test_max_iterations_guardrail_caps_agent_invocations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Guardrails.max_iterations counts TOTAL agent invocations across the
    run; the fourth begin (publisher) trips a cap of 3."""
    project = tmp_path / "team_hello"
    shutil.copytree(TEAM_DIR, project)
    system_yaml = project / "system.yaml"
    system_yaml.write_text(
        system_yaml.read_text().replace(
            "guardrails:\n  max_iterations: 12\n  max_hops: 12\n",
            "guardrails:\n  max_iterations: 3\n  max_hops: 12\n",
        )
    )
    transport = MarkerTransport(_looping_turns(5))
    run_id = str(RunId.new())
    code = execute_run(
        project,
        TEAM_INPUT,
        transport=transport.build(),
        checkpoint="memory",
        run_id=run_id,
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "IterationLimitError" in err
    assert "max_iterations" in err
