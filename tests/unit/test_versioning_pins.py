"""foundry.versioning.pins unit tests — surgical, transactional pin edits."""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.core.errors import ConfigValidationError, PinConflictError
from foundry.versioning.pins import (
    PinTransaction,
    read_prompt_pin,
    read_tool_pin,
    replace_nested_scalar,
)

_SYSTEM_YAML = """\
# hello — fixture manifest (comments must survive pin edits byte-for-byte).
name: hello
description: Fixture.
agents: [hello_agent]
flow:
  type: single
  agent: hello_agent
tools:
  get_time:
    ref: catalog/http_get_json
    version: v1   # trailing comment stays put
    connection_bindings:
      service: time_service
  banner:
    ref: local/banner
    version: v2
connections:
  time_service:
    ref: catalog/http_service
    version: v1
    config:
      base_url: https://example.test
    credentials_ref:
      kind: env
      value: FIXTURE_KEY
"""

_AGENT_YAML = """\
name: hello_agent
description: Fixture agent.
model_binding:
  provider: anthropic
  model: claude-haiku-4-5
  settings:
    max_tokens: 128
prompt:
  version: v2
  path: prompts/v2.md
output:
  schema: output_schema.py::Greeting
state_visibility:
  read: [name]
  write: [greeting]
"""


@pytest.fixture
def project(tmp_path: Path) -> Path:
    project = tmp_path / "projects" / "hello"
    (project / "agents" / "hello_agent").mkdir(parents=True)
    (project / "system.yaml").write_text(_SYSTEM_YAML)
    (project / "agents" / "hello_agent" / "agent.yaml").write_text(_AGENT_YAML)
    return project


# --- replace_nested_scalar -------------------------------------------------------------


@pytest.mark.unit
def test_replace_changes_exactly_one_line(project: Path) -> None:
    new_text, old = replace_nested_scalar(
        _SYSTEM_YAML, ["tools", "banner", "version"], "v1",
        file=project / "system.yaml",
    )
    assert old == "v2"
    diff = [
        (a, b)
        for a, b in zip(
            _SYSTEM_YAML.splitlines(), new_text.splitlines(), strict=True
        )
        if a != b
    ]
    assert diff == [("    version: v2", "    version: v1")]


@pytest.mark.unit
def test_replace_targets_the_right_sibling_and_keeps_comment(
    project: Path,
) -> None:
    """get_time's pin (with trailing comment) edits without touching
    banner's identical-looking `version:` line."""
    new_text, old = replace_nested_scalar(
        _SYSTEM_YAML, ["tools", "get_time", "version"], "v9",
        file=project / "system.yaml",
    )
    assert old == "v1"
    assert "    version: v9   # trailing comment stays put" in new_text
    assert new_text.count("version: v2") == 1  # banner untouched
    # the connection pin (also `version: v1`) is untouched
    assert "    version: v1\n    config:" in new_text


@pytest.mark.unit
def test_replace_missing_key_is_structured_error(project: Path) -> None:
    with pytest.raises(PinConflictError, match="could not locate"):
        replace_nested_scalar(
            _SYSTEM_YAML, ["tools", "nope", "version"], "v1",
            file=project / "system.yaml",
        )


@pytest.mark.unit
def test_replace_block_value_is_refused(project: Path) -> None:
    with pytest.raises(PinConflictError, match="inline scalar"):
        replace_nested_scalar(
            _SYSTEM_YAML, ["tools", "get_time", "connection_bindings"], "x",
            file=project / "system.yaml",
        )


# --- reads ----------------------------------------------------------------------------


@pytest.mark.unit
def test_pin_reads(project: Path) -> None:
    assert read_tool_pin(project, "get_time") == ("catalog/http_get_json", "v1")
    assert read_prompt_pin(project, "hello_agent") == ("v2", "prompts/v2.md")
    with pytest.raises(PinConflictError, match="not bound"):
        read_tool_pin(project, "nope")


# --- transaction ------------------------------------------------------------------------


@pytest.mark.unit
def test_tool_pin_edit_is_single_line(project: Path) -> None:
    txn = PinTransaction(project)
    change = txn.set_tool_version("banner", "v1")
    assert change.describe() == "/tools/banner/version: v2 -> v1"
    written = txn.apply()
    assert written == [project / "system.yaml"]
    before = _SYSTEM_YAML.splitlines()
    after = (project / "system.yaml").read_text().splitlines()
    assert sum(1 for a, b in zip(before, after, strict=True) if a != b) == 1
    assert read_tool_pin(project, "banner") == ("local/banner", "v1")


@pytest.mark.unit
def test_prompt_pin_edit_updates_version_and_path_together(
    project: Path,
) -> None:
    txn = PinTransaction(project)
    changes = txn.set_prompt_version("hello_agent", "v1")
    assert [c.pointer for c in changes] == ["/prompt/version", "/prompt/path"]
    txn.apply()
    assert read_prompt_pin(project, "hello_agent") == ("v1", "prompts/v1.md")
    # exactly two lines changed
    before = _AGENT_YAML.splitlines()
    after = (
        (project / "agents" / "hello_agent" / "agent.yaml")
        .read_text()
        .splitlines()
    )
    assert sum(1 for a, b in zip(before, after, strict=True) if a != b) == 2


@pytest.mark.unit
def test_transaction_is_all_or_nothing_across_files(project: Path) -> None:
    """An invalid staged edit anywhere writes NOTHING anywhere."""
    txn = PinTransaction(project)
    txn.set_tool_version("banner", "v1")
    # stage a prompt edit, then vandalise the staged agent text so schema
    # validation fails at apply time
    txn.set_prompt_version("hello_agent", "v1")
    agent_file = project / "agents" / "hello_agent" / "agent.yaml"
    txn._texts[agent_file] = "name: [broken\n"
    with pytest.raises(ConfigValidationError, match="nothing was written"):
        txn.apply()
    # both files untouched on disk
    assert (project / "system.yaml").read_text() == _SYSTEM_YAML
    assert agent_file.read_text() == _AGENT_YAML


@pytest.mark.unit
def test_empty_transaction_refused(project: Path) -> None:
    with pytest.raises(PinConflictError, match="no staged edits"):
        PinTransaction(project).apply()


@pytest.mark.unit
def test_invalid_version_string_refused(project: Path) -> None:
    with pytest.raises(PinConflictError, match="invalid version"):
        PinTransaction(project).set_tool_version("banner", "2")
