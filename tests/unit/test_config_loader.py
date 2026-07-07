"""Config loader tests: structured errors, hints, positions (docs/12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.config import load_agent_spec, load_state_spec, load_system_spec
from foundry.core.errors import ConfigLoadError, ConfigValidationError

VALID_AGENT_YAML = """\
name: hello_agent
description: greets people
model_binding:
  provider: anthropic
  model: claude-haiku-4-5
  settings:
    max_tokens: 512
prompt:
  version: v1
  path: prompts/v1.md
output:
  schema: output_schema.py::Greeting
state_visibility:
  read: [name]
  write: [greeting]
"""

VALID_SYSTEM_YAML = """\
name: hello
description: trivial single-agent greeting system
agents: [hello_agent]
flow:
  type: single
  agent: hello_agent
"""

VALID_STATE_YAML = """\
schema:
  name:
    type: str
  greeting:
    type: str
visibility:
  hello_agent:
    read: [name]
    write: [greeting]
"""


@pytest.mark.unit
def test_load_valid_agent_spec(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(VALID_AGENT_YAML)
    spec = load_agent_spec(path)
    assert spec.name == "hello_agent"
    assert spec.model_binding.provider == "anthropic"
    assert spec.model_binding.settings.max_tokens == 512
    assert spec.output.schema_ref == "output_schema.py::Greeting"
    assert spec.prompt.version == "v1"


@pytest.mark.unit
def test_load_valid_system_and_state(tmp_path: Path) -> None:
    (tmp_path / "system.yaml").write_text(VALID_SYSTEM_YAML)
    (tmp_path / "state.yaml").write_text(VALID_STATE_YAML)
    system = load_system_spec(tmp_path / "system.yaml")
    state = load_state_spec(tmp_path / "state.yaml")
    assert system.flow.type == "single"
    assert system.flow.agent == "hello_agent"
    assert state.state_schema["name"].type == "str"
    assert state.visibility["hello_agent"].read == ["name"]


@pytest.mark.unit
def test_missing_file_is_config_load_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError) as excinfo:
        load_agent_spec(tmp_path / "nope.yaml")
    assert "not found" in str(excinfo.value)
    assert "nope.yaml" in excinfo.value.context["file"]


@pytest.mark.unit
def test_typo_field_names_file_field_and_hint(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(VALID_AGENT_YAML.replace("model_binding:", "model_bindings:"))
    with pytest.raises(ConfigValidationError) as excinfo:
        load_agent_spec(path)
    message = str(excinfo.value)
    assert str(path) in message  # names the file
    assert "/model_bindings" in message  # names the field
    assert 'did you mean "model_binding"?' in message  # Levenshtein hint
    assert excinfo.value.context["line"] is not None


@pytest.mark.unit
def test_enum_near_miss_gets_hint(tmp_path: Path) -> None:
    path = tmp_path / "system.yaml"
    path.write_text(VALID_SYSTEM_YAML.replace("type: single", "type: singel"))
    with pytest.raises(ConfigValidationError) as excinfo:
        load_system_spec(path)
    assert 'did you mean "single"?' in str(excinfo.value)


@pytest.mark.unit
def test_wrong_type_reports_received_and_expected(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(VALID_AGENT_YAML.replace("max_tokens: 512", "max_tokens: lots"))
    with pytest.raises(ConfigValidationError) as excinfo:
        load_agent_spec(path)
    message = str(excinfo.value)
    assert "received" in message
    assert "lots" in message
    assert "/model_binding/settings/max_tokens" in message


@pytest.mark.unit
def test_yaml_syntax_error_names_line_and_column(tmp_path: Path) -> None:
    path = tmp_path / "system.yaml"
    path.write_text("flow: [unclosed\nname: hello")
    with pytest.raises(ConfigLoadError) as excinfo:
        load_system_spec(path)
    assert "line" in str(excinfo.value)
    assert excinfo.value.context.get("line") is not None


@pytest.mark.unit
def test_prompt_ref_path_must_match_version(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(VALID_AGENT_YAML.replace("path: prompts/v1.md", "path: prompts/v2.md"))
    with pytest.raises(ConfigValidationError) as excinfo:
        load_agent_spec(path)
    assert "does not match version" in str(excinfo.value)


@pytest.mark.unit
def test_system_spec_requires_agents_or_functions(tmp_path: Path) -> None:
    path = tmp_path / "system.yaml"
    path.write_text(VALID_SYSTEM_YAML.replace("agents: [hello_agent]", "agents: []"))
    with pytest.raises(ConfigValidationError) as excinfo:
        load_system_spec(path)
    assert "at least one of agents or functions" in str(excinfo.value)


@pytest.mark.unit
def test_round_trip_agent_spec(tmp_path: Path) -> None:
    path = tmp_path / "agent.yaml"
    path.write_text(VALID_AGENT_YAML)
    spec = load_agent_spec(path)
    dumped = spec.model_dump(by_alias=True, mode="json")
    from foundry.config import AgentSpec

    assert AgentSpec.model_validate(dumped) == spec
