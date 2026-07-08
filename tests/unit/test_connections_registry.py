"""Connection preparation + compile-time slot wiring (docs/23)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry.config import (
    ConnectionBinding,
    ConnectionSlot,
    FoundryRoots,
    ToolBinding,
    ToolSpec,
)
from foundry.connections import (
    prepare_connection,
    resolve_connection_credentials,
    validate_tool_connection_wiring,
)
from foundry.core import AuthScheme, CredentialsRef, ResolvedCredentials
from foundry.core.errors import (
    CompileError,
    ConnectionAuthError,
    ConnectionSlotNotBoundError,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeSecrets:
    def __init__(self, secret: str | None = "k-123") -> None:
        self._secret = secret

    def resolve(self, ref: CredentialsRef | None) -> ResolvedCredentials:
        return ResolvedCredentials(kind="env", secret=self._secret)


def _roots() -> FoundryRoots:
    return FoundryRoots(
        catalog_roots=[REPO_ROOT / "catalog"],
        projects_root=REPO_ROOT / "projects",
        project_name="hello",
    )


def _binding(**config: object) -> ConnectionBinding:
    return ConnectionBinding.model_validate(
        {
            "ref": "catalog/http_service",
            "version": "v1",
            "config": {"base_url": "https://svc.test", **config},
            "credentials_ref": {"kind": "env", "value": "FAKE_KEY"},
        }
    )


# --- credentials resolution -----------------------------------------------------


@pytest.mark.unit
def test_single_secret_maps_to_scheme_primary_field() -> None:
    creds = resolve_connection_credentials(
        CredentialsRef(kind="env", value="X"), AuthScheme.API_KEY, FakeSecrets("k-1")
    )
    assert creds.require("api_key").reveal() == "k-1"


@pytest.mark.unit
def test_json_secret_maps_to_multiple_fields_and_principal() -> None:
    payload = json.dumps(
        {"username": "svc_user", "password": "pw", "principal": "svc_user@corp"}
    )
    creds = resolve_connection_credentials(
        CredentialsRef(kind="env", value="X"),
        AuthScheme.BASIC_AUTH,
        FakeSecrets(payload),
    )
    assert creds.require("username").reveal() == "svc_user"
    assert creds.require("password").reveal() == "pw"
    assert creds.principal == "svc_user@corp"


@pytest.mark.unit
def test_multi_field_scheme_with_plain_secret_is_a_structured_error() -> None:
    with pytest.raises(ConnectionAuthError) as excinfo:
        resolve_connection_credentials(
            CredentialsRef(kind="env", value="X"),
            AuthScheme.BASIC_AUTH,
            FakeSecrets("just-a-password"),
        )
    assert "JSON object" in str(excinfo.value)


@pytest.mark.unit
def test_empty_credentials_allowed() -> None:
    creds = resolve_connection_credentials(
        None, AuthScheme.API_KEY, FakeSecrets(None)
    )
    assert creds.fields == {}


# --- prepare_connection ---------------------------------------------------------


@pytest.mark.unit
def test_prepare_connection_validates_config_and_builds_descriptor() -> None:
    prepared = prepare_connection("svc", _binding(), _roots(), FakeSecrets())
    assert prepared.canonical_ref == "catalog/http_service@v1"
    assert prepared.descriptor.auth_scheme is AuthScheme.API_KEY
    assert prepared.descriptor.redacted_config["base_url"] == "https://svc.test"
    # api_key_* fields are NOT in non_sensitive_config_fields → dropped
    assert "api_key_header" not in prepared.descriptor.redacted_config
    assert prepared.refresh.mode == "on_auth_error"
    # descriptor never carries the secret
    assert "k-123" not in prepared.descriptor.model_dump_json()


@pytest.mark.unit
def test_incompatible_config_names_missing_and_unexpected_fields() -> None:
    binding = ConnectionBinding.model_validate(
        {
            "ref": "catalog/http_service",
            "version": "v1",
            "config": {"warehouse": "wrong-system-field"},  # no base_url
            "credentials_ref": {"kind": "env", "value": "FAKE_KEY"},
        }
    )
    with pytest.raises(CompileError) as excinfo:
        prepare_connection("svc", binding, _roots(), FakeSecrets())
    assert excinfo.value.context["missing_fields"] == ["base_url"]
    assert excinfo.value.context["unexpected_fields"] == ["warehouse"]
    assert "README.md" in str(excinfo.value)  # hint points at the version's docs


# --- slot wiring validation ------------------------------------------------------


def _tool_spec(*, accepts: list[str], optional: bool = False) -> ToolSpec:
    return ToolSpec.model_validate(
        {
            "name": "demo_tool",
            "version": "v1",
            "description": "d",
            "input_schema": "schemas.py::In",
            "output_schema": "schemas.py::Out",
            "handler": "handler.py::handle",
            "connections_required": [
                {"slot": "service", "accepts": accepts, "optional": optional}
            ],
        }
    )


def _prepared() -> dict[str, object]:
    return {"svc": prepare_connection("svc", _binding(), _roots(), FakeSecrets())}


@pytest.mark.unit
def test_bound_slot_with_matching_accepts_wires() -> None:
    wired = validate_tool_connection_wiring(
        "demo_tool",
        _tool_spec(accepts=["catalog/http_service"]),
        ToolBinding.model_validate(
            {"ref": "catalog/x", "version": "v1",
             "connection_bindings": {"service": "svc"}}
        ),
        _prepared(),  # type: ignore[arg-type]
    )
    assert set(wired) == {"service"}


@pytest.mark.unit
def test_unbound_slot_raises_naming_the_slot() -> None:
    with pytest.raises(ConnectionSlotNotBoundError) as excinfo:
        validate_tool_connection_wiring(
            "demo_tool",
            _tool_spec(accepts=["catalog/http_service"]),
            ToolBinding.model_validate({"ref": "catalog/x", "version": "v1"}),
            _prepared(),  # type: ignore[arg-type]
        )
    message = str(excinfo.value)
    assert "slot 'service' is not bound" in message
    assert "hint:" in message
    assert excinfo.value.context["slot"] == "service"


@pytest.mark.unit
def test_optional_unbound_slot_is_fine() -> None:
    wired = validate_tool_connection_wiring(
        "demo_tool",
        _tool_spec(accepts=["catalog/http_service"], optional=True),
        ToolBinding.model_validate({"ref": "catalog/x", "version": "v1"}),
        _prepared(),  # type: ignore[arg-type]
    )
    assert wired == {}


@pytest.mark.unit
def test_accepts_mismatch_names_accepts_and_rejected_ref() -> None:
    with pytest.raises(CompileError) as excinfo:
        validate_tool_connection_wiring(
            "demo_tool",
            _tool_spec(accepts=["catalog/postgres", "catalog/pgvector"]),
            ToolBinding.model_validate(
                {"ref": "catalog/x", "version": "v1",
                 "connection_bindings": {"service": "svc"}}
            ),
            _prepared(),  # type: ignore[arg-type]
        )
    assert excinfo.value.context["accepts"] == [
        "catalog/postgres", "catalog/pgvector",
    ]
    assert excinfo.value.context["rejected_ref"] == "catalog/http_service@v1"


@pytest.mark.unit
def test_exact_version_accept_entry_enforced() -> None:
    with pytest.raises(CompileError):
        validate_tool_connection_wiring(
            "demo_tool",
            _tool_spec(accepts=["catalog/http_service@v2"]),  # bound is v1
            ToolBinding.model_validate(
                {"ref": "catalog/x", "version": "v1",
                 "connection_bindings": {"service": "svc"}}
            ),
            _prepared(),  # type: ignore[arg-type]
        )


@pytest.mark.unit
def test_binding_to_unknown_connection_name_rejected() -> None:
    with pytest.raises(CompileError) as excinfo:
        validate_tool_connection_wiring(
            "demo_tool",
            _tool_spec(accepts=["catalog/http_service"]),
            ToolBinding.model_validate(
                {"ref": "catalog/x", "version": "v1",
                 "connection_bindings": {"service": "ghost"}}
            ),
            _prepared(),  # type: ignore[arg-type]
        )
    assert excinfo.value.context["bound_name"] == "ghost"


@pytest.mark.unit
def test_binding_an_undeclared_slot_rejected() -> None:
    with pytest.raises(CompileError) as excinfo:
        validate_tool_connection_wiring(
            "demo_tool",
            _tool_spec(accepts=["catalog/http_service"]),
            ToolBinding.model_validate(
                {"ref": "catalog/x", "version": "v1",
                 "connection_bindings": {"service": "svc", "warehuose": "svc"}}
            ),
            _prepared(),  # type: ignore[arg-type]
        )
    assert excinfo.value.context["unknown_slots"] == ["warehuose"]


@pytest.mark.unit
def test_connection_slot_schema_validates_slot_names() -> None:
    with pytest.raises(ValueError):
        ConnectionSlot.model_validate({"slot": "Bad Slot", "accepts": ["x"]})
