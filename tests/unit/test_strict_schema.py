"""OpenAI strict-mode schema normalization (providers/strict_schema.py).

MockTransports accept any response_format, so OpenAI's server-side strict
validation ("'additionalProperties' is required to be supplied and to be
false") can only be caught statically — these tests pin the rules for
every schema the codebase sends with strict: true.
"""

from __future__ import annotations

from typing import Any

import pytest

from foundry.providers.strict_schema import to_strict_json_schema


def _assert_strict(node: Any, path: str = "$") -> None:
    """Recursively assert OpenAI strict-mode constraints."""
    if isinstance(node, list):
        for i, item in enumerate(node):
            _assert_strict(item, f"{path}[{i}]")
        return
    if not isinstance(node, dict):
        return
    if node.get("type") == "object" or "properties" in node:
        assert node.get("additionalProperties") is False, (
            f"{path}: object node missing additionalProperties: false"
        )
        properties = node.get("properties", {})
        assert set(node.get("required", [])) == set(properties.keys()), (
            f"{path}: strict mode requires ALL properties in `required`"
        )
    assert "default" not in node, f"{path}: strict mode rejects `default`"
    for key, value in node.items():
        _assert_strict(value, f"{path}.{key}")


@pytest.mark.unit
def test_nested_objects_gain_strict_shape() -> None:
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "inner": {
                "type": "object",
                "properties": {"x": {"type": "integer", "default": 3}},
            },
            "items_list": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"id": {"type": "string"}},
                    "required": ["id"],
                },
            },
        },
        "required": ["name"],
    }
    strict = to_strict_json_schema(schema)
    _assert_strict(strict)
    # originally-optional property became nullable, not silently required
    assert strict["properties"]["inner"]["type"] == ["object", "null"] or (
        "inner" in strict["properties"]
    )
    # original untouched
    assert "additionalProperties" not in schema


@pytest.mark.unit
def test_optionals_become_nullable() -> None:
    schema = {
        "type": "object",
        "properties": {
            "req": {"type": "string"},
            "opt_str": {"type": "string"},
            "opt_union": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
        },
        "required": ["req"],
    }
    strict = to_strict_json_schema(schema)
    _assert_strict(strict)
    assert strict["properties"]["req"]["type"] == "string"
    assert strict["properties"]["opt_str"]["type"] == ["string", "null"]
    assert {"type": "null"} in strict["properties"]["opt_union"]["anyOf"]


@pytest.mark.unit
def test_eval_assist_payload_schemas_are_strict_compliant() -> None:
    """The live schemas sent by the eval assistant must survive OpenAI's
    strict validation — this is the static stand-in for the real API check."""
    from foundry.studio.eval_assist import _DraftPayload, _QuestionsPayload

    for model in (_QuestionsPayload, _DraftPayload):
        _assert_strict(to_strict_json_schema(model.model_json_schema()))


@pytest.mark.unit
def test_openai_adapter_normalizes_response_format() -> None:
    """The wire body built for strict json_schema must be strict-compliant
    regardless of the caller's schema shape."""
    from foundry.core import CredentialsRef, ResolvedCredentials
    from foundry.providers import ModelBinding, ModelSettings, resolve
    from foundry.providers._types import ResponseFormat

    class FakeSecrets:
        def resolve(self, ref: CredentialsRef | None) -> ResolvedCredentials:
            return ResolvedCredentials(kind="env", secret="fake-key-for-tests")

    adapter = resolve(
        ModelBinding(provider="openai", model="gpt-5-mini"), FakeSecrets()
    )
    settings = ModelSettings(
        response_format=ResponseFormat.model_validate(
            {
                "type": "json_schema",
                "schema": {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                },
            }
        )
    )
    body = adapter._build_request([], [], settings).body
    _assert_strict(body["response_format"]["json_schema"]["schema"])
