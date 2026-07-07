"""Composition tests: extends (one-deep) + env interpolation (docs/12)."""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.config import load_system_spec
from foundry.core.errors import ConfigLoadError

BASE_SYSTEM = """\
name: base
description: base description
agents: [hello_agent]
flow:
  type: single
  agent: hello_agent
observability:
  trace: otel
"""


@pytest.mark.unit
def test_extends_shallow_merge(tmp_path: Path) -> None:
    (tmp_path / "base.yaml").write_text(BASE_SYSTEM)
    (tmp_path / "system.yaml").write_text(
        "extends: base.yaml\nname: overlay\n"
    )
    spec = load_system_spec(tmp_path / "system.yaml")
    assert spec.name == "overlay"  # overlay wins
    assert spec.description == "base description"  # base falls through
    assert spec.agents == ["hello_agent"]


@pytest.mark.unit
def test_extends_replaces_lists_not_extends_them(tmp_path: Path) -> None:
    (tmp_path / "base.yaml").write_text(BASE_SYSTEM)
    (tmp_path / "system.yaml").write_text(
        "extends: base.yaml\nagents: [other_agent]\n"
        "flow:\n  type: single\n  agent: other_agent\n"
    )
    spec = load_system_spec(tmp_path / "system.yaml")
    assert spec.agents == ["other_agent"]


@pytest.mark.unit
def test_extends_target_missing_is_load_error(tmp_path: Path) -> None:
    (tmp_path / "system.yaml").write_text("extends: nope.yaml\nname: x\n")
    with pytest.raises(ConfigLoadError) as excinfo:
        load_system_spec(tmp_path / "system.yaml")
    assert "extends target not found" in str(excinfo.value)
    assert "nope.yaml" in excinfo.value.context["extends"]


@pytest.mark.unit
def test_extends_is_one_deep_only(tmp_path: Path) -> None:
    (tmp_path / "grandbase.yaml").write_text(BASE_SYSTEM)
    (tmp_path / "base.yaml").write_text("extends: grandbase.yaml\nname: mid\n")
    (tmp_path / "system.yaml").write_text("extends: base.yaml\nname: leaf\n")
    with pytest.raises(ConfigLoadError) as excinfo:
        load_system_spec(tmp_path / "system.yaml")
    assert "one-deep" in str(excinfo.value)


@pytest.mark.unit
def test_env_interpolation_substitutes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FOUNDRY_TEST_TRACE", "off")
    (tmp_path / "system.yaml").write_text(
        BASE_SYSTEM.replace("trace: otel", "trace: ${ENV:FOUNDRY_TEST_TRACE}")
    )
    spec = load_system_spec(tmp_path / "system.yaml")
    assert spec.observability.trace == "off"


@pytest.mark.unit
def test_env_interpolation_default_used_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FOUNDRY_TEST_TRACE", raising=False)
    (tmp_path / "system.yaml").write_text(
        BASE_SYSTEM.replace(
            "trace: otel", "trace: ${ENV:FOUNDRY_TEST_TRACE:langsmith}"
        )
    )
    spec = load_system_spec(tmp_path / "system.yaml")
    assert spec.observability.trace == "langsmith"


@pytest.mark.unit
def test_env_interpolation_missing_without_default_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("FOUNDRY_TEST_TRACE", raising=False)
    (tmp_path / "system.yaml").write_text(
        BASE_SYSTEM.replace("trace: otel", "trace: ${ENV:FOUNDRY_TEST_TRACE}")
    )
    with pytest.raises(ConfigLoadError) as excinfo:
        load_system_spec(tmp_path / "system.yaml")
    assert "FOUNDRY_TEST_TRACE" in str(excinfo.value)
    assert excinfo.value.context["env_var"] == "FOUNDRY_TEST_TRACE"


@pytest.mark.unit
def test_env_interpolation_is_full_scalar_only(tmp_path: Path) -> None:
    # A partial placeholder inside a longer string is NOT substituted.
    (tmp_path / "system.yaml").write_text(
        BASE_SYSTEM.replace(
            "description: base description",
            "description: prefix-${ENV:MISSING_VAR}-suffix",
        )
    )
    spec = load_system_spec(tmp_path / "system.yaml")
    assert spec.description == "prefix-${ENV:MISSING_VAR}-suffix"
