"""OpenAI strict structured-output schema normalization.

OpenAI's ``response_format: json_schema`` with ``strict: true`` REJECTS
schemas unless every object node sets ``additionalProperties: false`` and
lists every property in ``required`` (HTTP 400: "'additionalProperties' is
required to be supplied and to be false"). Optionality must instead be
expressed as a union with ``null`` — which is exactly the transformation
``to_strict_json_schema`` applies, per OpenAI's documented recipe:

- every ``type: object`` node gains ``additionalProperties: false``;
- every property becomes required; properties that were NOT originally
  required get ``null`` added to their type (or an ``anyOf`` branch), so
  the model may still omit a value semantically;
- ``$defs`` / ``items`` / ``anyOf`` / ``allOf`` / ``oneOf`` are walked
  recursively; ``default`` keys are dropped (unsupported in strict mode).

The input schema is not mutated; a deep-transformed copy is returned.
Mock transports accept anything, so compliance is pinned by unit tests
rather than live calls (tests/unit/test_strict_schema.py).
"""

from __future__ import annotations

from typing import Any

__all__ = ["to_strict_json_schema"]


def to_strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Return a deep copy of ``schema`` satisfying OpenAI strict mode."""
    return _walk(schema)


def _walk(node: Any) -> Any:
    if isinstance(node, list):
        return [_walk(item) for item in node]
    if not isinstance(node, dict):
        return node

    out: dict[str, Any] = {}
    for key, value in node.items():
        if key == "default":
            # strict mode rejects `default`; optionality is handled below.
            continue
        if key == "properties" and isinstance(value, dict):
            out[key] = {name: _walk(sub) for name, sub in value.items()}
        else:
            out[key] = _walk(value)

    if out.get("type") == "object" or "properties" in out:
        out["additionalProperties"] = False
        properties = out.get("properties", {})
        originally_required = set(node.get("required") or [])
        for name, sub in properties.items():
            if name not in originally_required and isinstance(sub, dict):
                properties[name] = _nullable(sub)
        out["required"] = list(properties.keys())

    return out


def _nullable(sub: dict[str, Any]) -> dict[str, Any]:
    """Make a property schema accept null (strict-mode optionality)."""
    type_value = sub.get("type")
    if isinstance(type_value, str) and type_value != "null":
        return {**sub, "type": [type_value, "null"]}
    if isinstance(type_value, list):
        return sub if "null" in type_value else {**sub, "type": [*type_value, "null"]}
    if "anyOf" in sub and isinstance(sub["anyOf"], list):
        branches = sub["anyOf"]
        if not any(
            isinstance(b, dict) and b.get("type") == "null" for b in branches
        ):
            return {**sub, "anyOf": [*branches, {"type": "null"}]}
        return sub
    if "$ref" in sub:
        return {"anyOf": [sub, {"type": "null"}]}
    return sub
