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
    # hello binds openai/gpt-5-mini, so the shipped hello project now
    # exercises scripted_transport's openai branch.
    transport = scripted_transport(
        [json.dumps({"greeting": "Hello from the script!"})], provider="openai"
    )
    code = execute_run(HELLO_DIR, '{"name": "world"}', transport=transport)
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"greeting": "Hello from the script!"}

    run_dir = _single_run_dir(tmp_path)
    metadata = json.loads((run_dir / "metadata.json").read_text())
    assert metadata["status"] == "completed"
    assert metadata["provider"] == "openai"
    assert metadata["run_id"] == run_dir.name


@pytest.mark.integration
def test_scripted_transport_anthropic_shape(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The anthropic branch of scripted_transport, against an
    anthropic-bound copy of hello — hello itself moved to openai, so this
    keeps BOTH provider branches of the fixture covered.

    (Formerly test_scripted_transport_openai_shape, whose coverage is now
    carried by the hello end-to-end test above.)"""
    project = tmp_path / "hello_anthropic"
    shutil.copytree(HELLO_DIR, project)
    agent_yaml = project / "agents" / "hello_agent" / "agent.yaml"
    text = agent_yaml.read_text()
    text = text.replace("provider: openai", "provider: anthropic")
    text = text.replace("model: gpt-5-mini", "model: claude-haiku-4-5")
    agent_yaml.write_text(text)

    transport = scripted_transport(
        [json.dumps({"greeting": "Hi from scripted anthropic!"})],
        provider="anthropic",
    )
    code = execute_run(project, '{"name": "world"}', transport=transport)
    assert code == 0
    printed = json.loads(capsys.readouterr().out)
    assert printed == {"greeting": "Hi from scripted anthropic!"}

    metadata = json.loads((_single_run_dir(tmp_path) / "metadata.json").read_text())
    assert metadata["provider"] == "anthropic"
    assert metadata["model"] == "claude-haiku-4-5"
