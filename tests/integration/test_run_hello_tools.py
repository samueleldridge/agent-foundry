"""Phase 2a exit-gate integration tests against projects/hello + the catalog.

Live-key runs are the operator's manual step (docs/_manual_tests/phase_2a.md);
here the full real path — compile, tool registry, connection pool, auth,
LLM ⇄ tool loop — runs against httpx.MockTransport fakes with only the HTTP
layer substituted (the established Phase 1 pattern).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from foundry.cli.connections import execute_connections_health
from foundry.cli.run import execute_run

REPO_ROOT = Path(__file__).resolve().parents[2]
HELLO_DIR = REPO_ROOT / "projects" / "hello"

FAKE_SERVICE_KEY = "fake-service-key-for-tests"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", FAKE_SERVICE_KEY)
    # copies of hello live under tmp_path; point them at the repo catalog
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))


class Transport:
    """Scripted fake: openai turns + time-service responses, with request
    capture for auth/header assertions."""

    def __init__(
        self,
        llm_turns: list[dict[str, Any]],
        time_responses: list[httpx.Response] | None = None,
    ) -> None:
        self.llm_turns = llm_turns
        self.time_responses = time_responses or []
        self.llm_requests: list[dict[str, Any]] = []
        self.time_requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.openai.com":
            self.llm_requests.append(json.loads(request.content))
            return httpx.Response(200, json=self.llm_turns[len(self.llm_requests) - 1])
        if request.url.host == "worldtimeapi.org":
            self.time_requests.append(request)
            if self.time_responses:
                index = min(len(self.time_requests) - 1, len(self.time_responses) - 1)
                return self.time_responses[index]
            return httpx.Response(200, json={"datetime": "2026-07-08T12:34:56+00:00"})
        raise AssertionError(f"unexpected host: {request.url.host}")

    def build(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _tool_use_turn(*calls: dict[str, Any]) -> dict[str, Any]:
    return {
        "model": "gpt-5-mini",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "Checking.",
                    "tool_calls": list(calls),
                },
                "finish_reason": "tool_calls",
            }
        ],
        "usage": {"prompt_tokens": 50, "completion_tokens": 30},
    }


def _final_turn(greeting: str) -> dict[str, Any]:
    return {
        "model": "gpt-5-mini",
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"greeting": greeting}),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 90, "completion_tokens": 25},
    }


def _get_time_block(block_id: str = "tu_1") -> dict[str, Any]:
    return {
        "id": block_id,
        "type": "function",
        "function": {
            "name": "get_time",
            "arguments": json.dumps({"path": "/api/timezone/Etc/UTC"}),
        },
    }


def _run_dir(tmp_path: Path) -> Path:
    dirs = sorted((tmp_path / "foundry_home" / "runs").iterdir())
    assert dirs, "no run artifact written"
    return dirs[-1]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _copy_hello(tmp_path: Path, name: str) -> Path:
    project = tmp_path / name
    shutil.copytree(HELLO_DIR, project)
    return project


# --- hero path: model → tool (pooled, authenticated connection) → model ---------


@pytest.mark.integration
def test_one_tool_agent_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = Transport(
        [_tool_use_turn(_get_time_block()), _final_turn("Hello, world! 12:34 UTC.")]
    )
    code = execute_run(HELLO_DIR, '{"name": "world"}', transport=transport.build())
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"greeting": "Hello, world! 12:34 UTC."}

    # the tool hit the connection's service with the API key injected
    assert len(transport.time_requests) == 1
    assert (
        transport.time_requests[0].headers["Authorization"]
        == f"Bearer {FAKE_SERVICE_KEY}"
    )
    # the second LLM turn saw the tool result (openai wire: one
    # role=tool message per result, content is a plain string)
    second_turn = transport.llm_requests[1]
    tool_result = second_turn["messages"][-1]
    assert tool_result["role"] == "tool"
    assert tool_result["tool_call_id"] == "tu_1"
    assert "2026-07-08T12:34:56" in tool_result["content"]

    run_dir = _run_dir(tmp_path)
    tool_calls = _read_jsonl(run_dir / "tool_calls.jsonl")
    assert len(tool_calls) == 1
    assert tool_calls[0]["tool_ref"] == "catalog/http_get_json"
    assert tool_calls[0]["tool_version"] == "v1"
    assert tool_calls[0]["success"] is True

    events = _read_jsonl(run_dir / "events.jsonl")
    connection_events = [e for e in events if e["event"] == "connection"]
    assert [e["lifecycle"] for e in connection_events] == ["acquire"]
    descriptor = connection_events[0]["connection_descriptor"]
    assert descriptor["ref"] == "catalog/http_service@v1"
    assert descriptor["slot"] == "service"
    assert FAKE_SERVICE_KEY not in json.dumps(descriptor)

    metadata = json.loads((run_dir / "metadata.json").read_text())
    assert metadata["pins"]["tools"]["get_time"] == "catalog/http_get_json@v1"
    assert (
        metadata["pins"]["connections"]["time_service"]
        == "catalog/http_service@v1"
    )
    assert metadata["connection_pool"]["builds"] == 1

    # no secret material anywhere in the artifact
    combined = "".join(
        p.read_text() for p in run_dir.iterdir() if p.is_file()
    )
    assert FAKE_SERVICE_KEY not in combined
    assert "fake-openai-key-for-tests" not in combined


@pytest.mark.integration
def test_run_totals_accumulate_across_llm_calls(tmp_path: Path) -> None:
    """A 2-round tool run's run.completed (and metadata trail) must report
    the SUM of both LLM calls' tokens/cost, not the last call's values
    (Phase 3 review finding 2: RunCounters kept only last_response)."""
    transport = Transport(
        [_tool_use_turn(_get_time_block()), _final_turn("Hello, totals!")]
    )
    code = execute_run(HELLO_DIR, '{"name": "world"}', transport=transport.build())
    assert code == 0

    run_dir = _run_dir(tmp_path)
    llm_calls = _read_jsonl(run_dir / "llm_calls.jsonl")
    assert len(llm_calls) == 2
    events = _read_jsonl(run_dir / "events.jsonl")
    completed = events[-1]
    assert completed["event"] == "run.completed"
    # round 1: 50 in / 30 out; round 2: 90 in / 25 out (scripted above)
    assert completed["total_input_tokens"] == 50 + 90
    assert completed["total_output_tokens"] == 30 + 25
    from decimal import Decimal

    expected_cost = sum(
        Decimal(call["cost_estimate_usd"]) for call in llm_calls
    )
    assert Decimal(completed["total_cost_estimate_usd"]) == expected_cost


@pytest.mark.integration
def test_pool_reuse_across_two_tool_calls_in_one_run(tmp_path: Path) -> None:
    transport = Transport(
        [
            _tool_use_turn(_get_time_block("tu_1"), _get_time_block("tu_2")),
            _final_turn("Hello twice!"),
        ]
    )
    code = execute_run(HELLO_DIR, '{"name": "world"}', transport=transport.build())
    assert code == 0
    run_dir = _run_dir(tmp_path)
    tool_calls = _read_jsonl(run_dir / "tool_calls.jsonl")
    assert len(tool_calls) == 2
    metadata = json.loads((run_dir / "metadata.json").read_text())
    pool = metadata["connection_pool"]
    assert pool["builds"] == 1, "two calls sharing a slot must reuse one client"
    assert pool["cache_hits"] == 1
    events = _read_jsonl(run_dir / "events.jsonl")
    lifecycles = sorted(
        e["lifecycle"] for e in events if e["event"] == "connection"
    )
    assert lifecycles == ["acquire", "cache_hit"]


# --- pin swaps -----------------------------------------------------------------


@pytest.mark.integration
def test_tool_pin_v1_to_v2_changes_loaded_version(tmp_path: Path) -> None:
    project = _copy_hello(tmp_path, "hello_v2")
    system = project / "system.yaml"
    system.write_text(
        system.read_text().replace(
            "ref: catalog/http_get_json\n    version: v1",
            "ref: catalog/http_get_json\n    version: v2",
        )
    )
    transport = Transport(
        [_tool_use_turn(_get_time_block()), _final_turn("Hello v2!")]
    )
    code = execute_run(project, '{"name": "world"}', transport=transport.build())
    assert code == 0
    tool_calls = _read_jsonl(_run_dir(tmp_path) / "tool_calls.jsonl")
    assert tool_calls[0]["tool_version"] == "v2"
    # v2 behaviour: the tool_result carries the resolved request URL
    tool_result = transport.llm_requests[1]["messages"][-1]
    assert tool_result["role"] == "tool"
    assert "worldtimeapi.org" in tool_result["content"]


@pytest.mark.integration
def test_connection_pin_v1_to_v2_switches_auth_scheme(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_hello(tmp_path, "hello_conn_v2")
    system = project / "system.yaml"
    text = system.read_text().replace(
        "ref: catalog/http_service\n    version: v1",
        "ref: catalog/http_service\n    version: v2",
    )
    # v2's config schema has no api_key_* fields and uses basic auth creds
    monkeypatch.setenv(
        "HELLO_SERVICE_API_KEY",
        json.dumps({"username": "svc", "password": "pw-basic"}),
    )
    system.write_text(text)
    transport = Transport(
        [_tool_use_turn(_get_time_block()), _final_turn("Hello basic!")]
    )
    code = execute_run(project, '{"name": "world"}', transport=transport.build())
    assert code == 0
    auth_header = transport.time_requests[0].headers["Authorization"]
    assert auth_header.startswith("Basic ")
    metadata = json.loads((_run_dir(tmp_path) / "metadata.json").read_text())
    assert (
        metadata["pins"]["connections"]["time_service"]
        == "catalog/http_service@v2"
    )


# --- refresh on auth error --------------------------------------------------------


@pytest.mark.integration
def test_401_evicts_and_rebuilds_connection_then_succeeds(tmp_path: Path) -> None:
    transport = Transport(
        [_tool_use_turn(_get_time_block()), _final_turn("Recovered!")],
        time_responses=[
            httpx.Response(401, json={"error": "expired key"}),
            httpx.Response(200, json={"datetime": "2026-07-08T12:00:00+00:00"}),
        ],
    )
    code = execute_run(HELLO_DIR, '{"name": "world"}', transport=transport.build())
    assert code == 0
    assert len(transport.time_requests) == 2  # 401 then retry on rebuilt client
    run_dir = _run_dir(tmp_path)
    metadata = json.loads((run_dir / "metadata.json").read_text())
    pool = metadata["connection_pool"]
    assert pool["builds"] == 2 and pool["evictions"] == 1
    tool_calls = _read_jsonl(run_dir / "tool_calls.jsonl")
    assert tool_calls[0]["success"] is True


# --- adversarial: allowlist ---------------------------------------------------------


@pytest.mark.integration
def test_tool_not_in_allowlist_refused_and_surfaced_to_llm(tmp_path: Path) -> None:
    project = _copy_hello(tmp_path, "hello_no_allow")
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    agent_yaml.write_text(agent_yaml.read_text().replace("tools: [get_time]", "tools: []"))
    # model hallucinates the tool call anyway
    transport = Transport(
        [_tool_use_turn(_get_time_block()), _final_turn("Sorry, no tools.")]
    )
    code = execute_run(project, '{"name": "world"}', transport=transport.build())
    assert code == 0  # dispatcher refuses; error goes to the LLM, run recovers
    assert transport.time_requests == []  # tool never ran
    tool_result = transport.llm_requests[1]["messages"][-1]
    assert tool_result["role"] == "tool"
    # openai's wire has no is_error flag; the error surfaces as the typed
    # boundary's error attribute — equal-strength check on the same contract.
    assert 'error="true"' in tool_result["content"]
    assert "ToolNotAllowedError" in tool_result["content"]
    assert "hello_agent" in tool_result["content"]


@pytest.mark.integration
def test_allowlisting_unknown_tool_fails_compile(tmp_path: Path) -> None:
    project = _copy_hello(tmp_path, "hello_unknown_tool")
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text().replace(
            "tools: [get_time]", "tools: [get_time, ghost_tool]"
        )
    )
    code = execute_run(project, '{"name": "world"}')
    assert code == 2


# --- adversarial: tool output validation -------------------------------------------


@pytest.mark.integration
def test_invalid_tool_output_raises_structured_error_to_llm(tmp_path: Path) -> None:
    """Tool output is validated at the boundary: a body shape that violates
    HttpGetOut (status_code must be int) surfaces ToolOutputValidationError."""
    project = _copy_hello(tmp_path, "hello_bad_tool")
    # local broken tool: returns a shape violating its declared output schema
    tool_dir = project / "tools" / "broken_time" / "v1"
    tool_dir.mkdir(parents=True)
    (tool_dir / "tool.yaml").write_text(
        "name: broken_time\nversion: v1\ndescription: returns garbage\n"
        "input_schema: schemas.py::In\noutput_schema: schemas.py::Out\n"
        "handler: handler.py::handle\nschema_version: 1\n"
    )
    (tool_dir / "schemas.py").write_text(
        "from pydantic import BaseModel\n\n"
        "class In(BaseModel):\n    pass\n\n"
        "class Out(BaseModel):\n    value: int\n"
    )
    (tool_dir / "handler.py").write_text(
        "async def handle(inputs, ctx):\n"
        "    return {'value': 'not-an-int-and-not-coercible'}\n"
    )
    (tool_dir / "eval.yaml").write_text(
        "name: e\nscope: tool\ntarget: local/broken_time@v1\n"
        "cases: [{id: c, input: {}, expected: {}}]\n"
        "scorers: [{kind: exact, name: s}]\nschema_version: 1\n"
    )
    (tool_dir / "README.md").write_text("# broken_time\n")
    system = project / "system.yaml"
    system.write_text(
        system.read_text().replace(
            "tools:\n",
            "tools:\n  broken_time:\n    ref: local/broken_time\n    version: v1\n",
            1,
        )
    )
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text().replace(
            "tools: [get_time]", "tools: [get_time, broken_time]"
        )
    )
    transport = Transport(
        [
            _tool_use_turn(
                {
                    "id": "tu_9",
                    "type": "function",
                    "function": {
                        "name": "broken_time",
                        "arguments": json.dumps({}),
                    },
                }
            ),
            _final_turn("Tool misbehaved."),
        ]
    )
    code = execute_run(project, '{"name": "world"}', transport=transport.build())
    assert code == 0
    tool_result = transport.llm_requests[1]["messages"][-1]
    assert tool_result["role"] == "tool"
    # openai's wire has no is_error flag; the typed boundary's error
    # attribute carries the same contract.
    assert 'error="true"' in tool_result["content"]
    assert "ToolOutputValidationError" in tool_result["content"]
    tool_calls = _read_jsonl(_run_dir(tmp_path) / "tool_calls.jsonl")
    assert tool_calls[0]["error_category"] == "ToolOutputValidationError"


# --- compile-time wiring errors ------------------------------------------------------


@pytest.mark.integration
def test_unbound_slot_fails_compile_naming_slot(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_hello(tmp_path, "hello_unbound")
    system = project / "system.yaml"
    system.write_text(
        system.read_text().replace(
            "    connection_bindings:\n      service: time_service\n", ""
        )
    )
    code = execute_run(project, '{"name": "world"}')
    assert code == 2
    err = capsys.readouterr().err
    assert "ConnectionSlotNotBoundError" in err
    assert "slot 'service' is not bound" in err
    assert "system.yaml" in err
    # compile-time: no run artifact was created
    assert not (tmp_path / "foundry_home" / "runs").exists()


@pytest.mark.integration
def test_accepts_mismatch_fails_compile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_hello(tmp_path, "hello_mismatch")
    system = project / "system.yaml"
    # bind the slot to a cohere_rerank connection — not in the accepts list
    text = system.read_text().replace("service: time_service", "service: reranker")
    text = text.replace(
        "connections:\n",
        "connections:\n"
        "  reranker:\n"
        "    ref: catalog/cohere_rerank\n"
        "    version: v1\n"
        "    credentials_ref:\n"
        "      kind: env\n"
        "      value: FAKE_COHERE_KEY\n",
        1,
    )
    monkeypatch.setenv("FAKE_COHERE_KEY", "fake-cohere-key")
    system.write_text(text)
    code = execute_run(project, '{"name": "world"}')
    assert code == 2
    err = capsys.readouterr().err
    assert "does not accept the bound connection" in err
    assert "catalog/cohere_rerank@v1" in err
    assert "catalog/http_service" in err  # the accepts list


@pytest.mark.integration
def test_missing_pinned_version_fails_compile_listing_available(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_hello(tmp_path, "hello_v9")
    system = project / "system.yaml"
    system.write_text(
        system.read_text().replace(
            "ref: catalog/http_get_json\n    version: v1",
            "ref: catalog/http_get_json\n    version: v9",
        )
    )
    code = execute_run(project, '{"name": "world"}')
    assert code == 2
    err = capsys.readouterr().err
    assert "v9" in err and "available" in err
    assert "v1" in err and "v2" in err


# --- secret-literal scan --------------------------------------------------------------


@pytest.mark.integration
def test_secret_literal_in_connection_config_rejected_at_load(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_hello(tmp_path, "hello_secret")
    system = project / "system.yaml"
    system.write_text(
        system.read_text().replace(
            "    config:\n",
            '    config:\n      api_key: "sk-ant-fake-credential-1234567890abcdef"\n',
        )
    )
    code = execute_run(project, '{"name": "world"}')
    assert code == 2
    err = capsys.readouterr().err
    assert "ConfigLoadError" in err
    assert "secret literal" in err
    assert "sk-ant-fake-credential" not in err  # never echo the value


# --- state visibility -------------------------------------------------------------------


@pytest.mark.integration
def test_reading_undeclared_state_field_fails_compile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_hello(tmp_path, "hello_vis")
    state = project / "state.yaml"
    state.write_text(
        state.read_text().replace(
            "visibility:",
            "  draft_plan:\n    type: str | None\n    description: hidden field\n"
            "visibility:",
            1,
        )
    )
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    # agent tries to read draft_plan; state.yaml's visibility doesn't grant it
    agent_yaml.write_text(
        agent_yaml.read_text().replace("read: [name]", "read: [name, draft_plan]")
    )
    code = execute_run(project, '{"name": "world"}')
    assert code == 2
    err = capsys.readouterr().err
    assert "StateVisibilityError" in err
    assert "draft_plan" in err
    assert "hello_agent" in err


@pytest.mark.integration
def test_visibility_hole_in_state_yaml_fails_compile(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = _copy_hello(tmp_path, "hello_hole")
    state = project / "state.yaml"
    state.write_text(
        state.read_text().replace("hello_agent:", "some_other_agent:")
    )
    code = execute_run(project, '{"name": "world"}')
    assert code == 2
    err = capsys.readouterr().err
    assert "StateVisibilityError" in err
    assert "hello_agent" in err


# --- connections health CLI -----------------------------------------------------------


@pytest.mark.integration
def test_connections_health_runs_health_yaml(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "worldtimeapi.org"
        assert request.url.path == "/api/ip"
        assert request.headers["Authorization"] == f"Bearer {FAKE_SERVICE_KEY}"
        return httpx.Response(200, json={"ok": True})

    code = execute_connections_health(
        str(HELLO_DIR / "time_service"), transport=httpx.MockTransport(handler)
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "time_service" in out and "OK" in out and "ping" in out


@pytest.mark.integration
def test_connections_health_fails_nonzero_on_unhealthy(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"error": "down"})

    code = execute_connections_health(
        str(HELLO_DIR), transport=httpx.MockTransport(handler)
    )
    assert code == 1
    err = capsys.readouterr().err
    assert "ConnectionHealthCheckError" in err
