"""`foundry.testing.scripted_transport` drives a compiled project end-to-end.

Mirrors tests/integration/test_run_hello.py's mocked-provider path, but the
transport comes from the shipped fixture instead of a hand-rolled handler —
this is exactly how project integration tests are meant to use it (docs/82).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from foundry.cli.run import execute_run
from foundry.testing import scripted_transport

HELLO_DIR = Path(__file__).resolve().parents[2] / "projects" / "hello"


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(HELLO_DIR.parents[1] / "catalog"))


def _single_run_dir(tmp_path: Path) -> Path:
    dirs = list((tmp_path / "foundry_home" / "runs").iterdir())
    assert len(dirs) == 1
    return dirs[0]


@pytest.mark.integration
def test_scripted_transport_runs_hello_end_to_end(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = scripted_transport(
        [json.dumps({"greeting": "Hello from the script!"})]
    )
    code = execute_run(HELLO_DIR, '{"name": "world"}', transport=transport)
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"greeting": "Hello from the script!"}

    run_dir = _single_run_dir(tmp_path)
    metadata = json.loads((run_dir / "metadata.json").read_text())
    assert metadata["status"] == "completed"
    assert metadata["provider"] == "anthropic"
    assert metadata["run_id"] == run_dir.name


@pytest.mark.integration
def test_scripted_transport_openai_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    project = tmp_path / "hello_openai"
    shutil.copytree(HELLO_DIR, project)
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    text = agent_yaml.read_text()
    text = text.replace("provider: anthropic", "provider: openai")
    text = text.replace("model: claude-haiku-4-5", "model: gpt-4o-mini")
    agent_yaml.write_text(text)

    transport = scripted_transport(
        [json.dumps({"greeting": "Hi from scripted openai!"})], provider="openai"
    )
    code = execute_run(project, '{"name": "world"}', transport=transport)
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"greeting": "Hi from scripted openai!"}

    metadata = json.loads((_single_run_dir(tmp_path) / "metadata.json").read_text())
    assert metadata["provider"] == "openai"
    assert metadata["model"] == "gpt-4o-mini"
