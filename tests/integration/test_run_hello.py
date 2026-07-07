"""Phase 1 exit-gate integration tests against projects/hello.

Live-key runs are the operator's manual step (docs/_manual_tests/phase_1.md);
here the full real path runs against httpx.MockTransport fakes for both
providers — only the HTTP layer is substituted.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import httpx
import pytest

from foundry.cli.run import execute_run

HELLO_DIR = Path(__file__).resolve().parents[2] / "projects" / "hello"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-tests")


def _anthropic_transport(greeting: str = "Hello, world!") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.anthropic.com"
        return httpx.Response(
            200,
            json={
                "content": [
                    {"type": "text", "text": json.dumps({"greeting": greeting})}
                ],
                "stop_reason": "end_turn",
                "model": "claude-haiku-4-5",
                "usage": {"input_tokens": 50, "output_tokens": 20},
            },
        )

    return httpx.MockTransport(handler)


def _openai_transport(greeting: str = "Hi from openai!") -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.openai.com"
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"greeting": greeting}),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 12},
            },
        )

    return httpx.MockTransport(handler)


def _runs_root(tmp_path: Path) -> Path:
    return tmp_path / "foundry_home" / "runs"


def _single_run_dir(tmp_path: Path) -> Path:
    dirs = list(_runs_root(tmp_path).iterdir())
    assert len(dirs) == 1
    return dirs[0]


def _swap_provider(project_dir: Path, provider: str, model: str) -> None:
    agent_yaml = project_dir / "agents" / "hello_agent" / "agent.yaml"
    text = agent_yaml.read_text()
    text = text.replace("provider: anthropic", f"provider: {provider}")
    text = text.replace("model: claude-haiku-4-5", f"model: {model}")
    agent_yaml.write_text(text)


@pytest.mark.integration
def test_hello_runs_end_to_end_against_anthropic_fake(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = execute_run(
        HELLO_DIR, '{"name": "world"}', transport=_anthropic_transport()
    )
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"greeting": "Hello, world!"}

    run_dir = _single_run_dir(tmp_path)
    metadata = json.loads((run_dir / "metadata.json").read_text())
    assert metadata["status"] == "completed"
    assert metadata["provider"] == "anthropic"
    assert metadata["run_id"] == run_dir.name

    llm_calls = [
        json.loads(line)
        for line in (run_dir / "llm_calls.jsonl").read_text().splitlines()
    ]
    assert len(llm_calls) == 1
    assert llm_calls[0]["token_usage"]["reasoning_tokens"] == 0
    assert llm_calls[0]["run_id"] == run_dir.name


@pytest.mark.integration
def test_provider_swap_is_a_yaml_only_change(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "hello_openai"
    shutil.copytree(HELLO_DIR, project)
    _swap_provider(project, "openai", "gpt-4o-mini")

    code = execute_run(project, '{"name": "world"}', transport=_openai_transport())
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"greeting": "Hi from openai!"}

    metadata = json.loads((_single_run_dir(tmp_path) / "metadata.json").read_text())
    assert metadata["provider"] == "openai"
    assert metadata["model"] == "gpt-4o-mini"


@pytest.mark.integration
def test_unknown_provider_structured_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "hello_foo"
    shutil.copytree(HELLO_DIR, project)
    _swap_provider(project, "foo", "some-model")

    code = execute_run(project, '{"name": "world"}')
    assert code == 2
    err = capsys.readouterr().err
    assert "unknown provider 'foo'; available: anthropic, openai" in err
    assert "agents/hello_agent/agent.yaml" in err  # names the file
    assert "/model_binding/provider" in err  # names the field
    assert "Traceback" not in err


@pytest.mark.integration
def test_invalid_yaml_shape_names_file_field_reason(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "hello_typo"
    shutil.copytree(HELLO_DIR, project)
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    agent_yaml.write_text(
        agent_yaml.read_text().replace("model_binding:", "model_bindings:")
    )

    code = execute_run(project, "{}")
    assert code == 2
    err = capsys.readouterr().err
    assert str(agent_yaml) in err
    assert "/model_bindings" in err
    assert 'did you mean "model_binding"?' in err


@pytest.mark.integration
def test_cost_budget_exceeded_pre_call_terminates_run_failed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "hello_budget"
    shutil.copytree(HELLO_DIR, project)
    system_yaml = project / "system.yaml"
    system_yaml.write_text(
        system_yaml.read_text().replace(
            "guardrails:\n  max_iterations: 5",
            'guardrails:\n  max_iterations: 5\n  max_cost_usd: "0.000001"',
        )
    )

    http_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json={})

    code = execute_run(
        project, '{"name": "world"}', transport=httpx.MockTransport(handler)
    )
    assert code == 1
    assert http_calls == 0, "budget must fire BEFORE any provider HTTP call"
    err = capsys.readouterr().err
    assert "CostBudgetExceeded" in err

    run_dir = _single_run_dir(tmp_path)
    # terminal event is run.failed with the budget context in the audit trail
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]
    assert events[-1]["event"] == "run.failed"
    assert events[-1]["error"]["error_class"] == "CostBudgetExceeded"
    assert "max_usd" in events[-1]["error"]["context"]
    # no LLM call ever recorded
    assert not (run_dir / "llm_calls.jsonl").exists()
    metadata = json.loads((run_dir / "metadata.json").read_text())
    assert metadata["status"] == "failed"
    assert metadata["cost_budget"]["max_usd"] == "0.000001"


@pytest.mark.integration
def test_run_id_threads_through_events_and_artifacts(tmp_path: Path) -> None:
    code = execute_run(
        HELLO_DIR, '{"name": "world"}', transport=_anthropic_transport()
    )
    assert code == 0
    run_dir = _single_run_dir(tmp_path)
    events = [
        json.loads(line)
        for line in (run_dir / "events.jsonl").read_text().splitlines()
    ]
    assert all(e["run_id"] == run_dir.name for e in events)
    # event-stream invariants: monotonic sequence, started first, terminal last
    assert [e["sequence"] for e in events] == list(range(len(events)))
    assert events[0]["event"] == "run.started"
    assert events[-1]["event"] == "run.completed"
    names = [e["event"] for e in events]
    assert names.count("agent.started") == names.count("agent.completed") == 1
    assert names.count("llm.started") == names.count("llm.completed") == 1


@pytest.mark.integration
def test_no_secret_material_in_artifacts_or_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    execute_run(HELLO_DIR, '{"name": "world"}', transport=_anthropic_transport())
    out = capsys.readouterr()
    combined = out.out + out.err
    run_dir = _single_run_dir(tmp_path)
    for f in run_dir.iterdir():
        combined += f.read_text()
    assert "fake-anthropic-key-for-tests" not in combined
