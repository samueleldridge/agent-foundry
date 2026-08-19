"""Secret scan + SecretsProvider tests (docs/12 § Secrets).

NOTE: no real secrets anywhere — all fixture values are synthetic strings
assembled at runtime to exercise the detection patterns.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from foundry.config import EnvSecretsProvider, load_system_spec
from foundry.core import CredentialsRef
from foundry.core.errors import ConfigLoadError

SYSTEM_TEMPLATE = """\
name: hello
description: test
agents: [hello_agent]
flow:
  type: single
  agent: hello_agent
metadata:
  note: {value}
"""


def _fake(prefix: str, filler: str = "0123456789abcdef0123") -> str:
    # Assemble at runtime so nothing in the repo looks like a credential.
    return prefix + filler


@pytest.mark.unit
@pytest.mark.parametrize(
    "value",
    [
        _fake("AKIA", "ABCDEFGHIJKLMNOP"),  # AWS access key shape
        _fake("sk-" + "ant-"),  # Anthropic key prefix
        _fake("sk-"),  # OpenAI-style key prefix
    ],
    ids=["aws", "anthropic", "openai"],
)
def test_secret_value_patterns_detected(tmp_path: Path, value: str) -> None:
    path = tmp_path / "system.yaml"
    path.write_text(SYSTEM_TEMPLATE.format(value=value))
    with pytest.raises(ConfigLoadError) as excinfo:
        load_system_spec(path)
    message = str(excinfo.value)
    assert "secret literal" in message
    assert value not in message, "error must never echo the secret value"
    assert value not in str(excinfo.value.context)


@pytest.mark.unit
def test_sensitive_key_name_heuristic_detected(tmp_path: Path) -> None:
    path = tmp_path / "system.yaml"
    path.write_text(
        SYSTEM_TEMPLATE.replace("note: {value}", "api_key: hunter2hunter2")
    )
    with pytest.raises(ConfigLoadError) as excinfo:
        load_system_spec(path)
    assert "api_key" in str(excinfo.value)


@pytest.mark.unit
def test_env_var_name_under_sensitive_key_is_allowed(tmp_path: Path) -> None:
    # UPPER_SNAKE values are references, not literals.
    path = tmp_path / "system.yaml"
    path.write_text(
        SYSTEM_TEMPLATE.replace("note: {value}", "api_key: ANTHROPIC_API_KEY")
    )
    load_system_spec(path)  # must not raise


@pytest.mark.unit
def test_allow_literal_pragma_suppresses_detection(tmp_path: Path) -> None:
    path = tmp_path / "system.yaml"
    path.write_text(
        SYSTEM_TEMPLATE.replace(
            "note: {value}",
            "api_key: hunter2hunter2  # foundry:allow-literal",
        )
    )
    load_system_spec(path)  # must not raise


# --- EnvSecretsProvider ---------------------------------------------------------


@pytest.mark.unit
def test_env_provider_resolves_env_ref(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_TEST_SECRET", "s3cr3t-value")
    creds = EnvSecretsProvider().resolve(
        CredentialsRef(kind="env", value="FOUNDRY_TEST_SECRET")
    )
    assert creds.secret == "s3cr3t-value"
    assert "s3cr3t-value" not in repr(creds), "repr must redact"


@pytest.mark.unit
def test_env_provider_missing_var_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_TEST_SECRET", raising=False)
    with pytest.raises(ConfigLoadError) as excinfo:
        EnvSecretsProvider().resolve(
            CredentialsRef(kind="env", value="FOUNDRY_TEST_SECRET")
        )
    assert "FOUNDRY_TEST_SECRET" in str(excinfo.value)


@pytest.mark.unit
@pytest.mark.parametrize("empty_value", ["", "   "])
def test_env_provider_empty_var_errors_like_unset(
    monkeypatch: pytest.MonkeyPatch, empty_value: str
) -> None:
    """Set-but-empty (`OPENAI_API_KEY=`) must fail like unset — never an
    empty Bearer header downstream."""
    monkeypatch.setenv("FOUNDRY_TEST_SECRET", empty_value)
    with pytest.raises(ConfigLoadError) as excinfo:
        EnvSecretsProvider().resolve(
            CredentialsRef(kind="env", value="FOUNDRY_TEST_SECRET")
        )
    assert "FOUNDRY_TEST_SECRET" in str(excinfo.value)
    assert "empty" in str(excinfo.value)
    assert excinfo.value.context["env_var"] == "FOUNDRY_TEST_SECRET"


@pytest.mark.unit
def test_env_provider_default_kind_returns_empty_credential() -> None:
    creds = EnvSecretsProvider().resolve(CredentialsRef(kind="default"))
    assert creds.secret is None
    assert creds.kind == "default"


@pytest.mark.unit
def test_env_provider_none_ref_behaves_like_default() -> None:
    creds = EnvSecretsProvider().resolve(None)
    assert creds.secret is None


@pytest.mark.unit
def test_env_provider_unsupported_kind_errors() -> None:
    with pytest.raises(ConfigLoadError) as excinfo:
        EnvSecretsProvider().resolve(
            CredentialsRef(kind="aws_profile", value="prod")
        )
    assert "not supported in Phase 1" in str(excinfo.value)
