"""Compile-time state compilation + per-node visibility enforcement.

Implements docs/22: parse StateSpec field-type strings into Python types,
build the project's Pydantic state model with reducer annotations, validate
per-agent visibility declarations, and generate per-agent TypedDict views.
The enforcement is STRUCTURAL — an agent's view of state is a projection in
which forbidden fields are literally absent, not a runtime check.
"""

from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Annotated, Any, TypedDict
from uuid import UUID

from pydantic import BaseModel, Field, create_model

from foundry.config import FieldSpec, StateSpec, StateVisibility
from foundry.core import FoundryMessage, Reducer, StateBase
from foundry.core.errors import ConfigError, StateVisibilityError
from foundry.core.retrieval import RetrievedDocument

_PRIMITIVES: dict[str, type] = {
    "str": str,
    "int": int,
    "float": float,
    "bool": bool,
    "bytes": bytes,
    "datetime": datetime,
    "date": date,
    "time": time,
    "timedelta": timedelta,
    "Decimal": Decimal,
    "UUID": UUID,
    "Any": Any,
    "FoundryMessage": FoundryMessage,
    "RetrievedDocument": RetrievedDocument,
}

_CONTAINER_RE = re.compile(r"^(list|dict|set|tuple)\[(.+)\]$")
_USER_MODEL_RE = re.compile(r"^BaseModel:(?P<module>[\w.]+):(?P<cls>\w+)$")


def _split_args(inner: str) -> list[str]:
    """Split 'str, int' / 'str, dict[str, int]' on top-level commas."""
    parts: list[str] = []
    depth = 0
    current = ""
    for char in inner:
        if char == "[":
            depth += 1
        elif char == "]":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def parse_type_string(type_str: str, *, where: str = "state.yaml") -> Any:
    """Parse a StateSpec field-type string into a Python type
    (docs/22 § Field types). Unparseable → ConfigError naming the string."""
    text = type_str.strip()

    # Optional: '<T> | None' (also tolerate 'None | <T>')
    if "|" in text:
        parts = [p.strip() for p in _split_args(text.replace("|", ","))]
        non_none = [p for p in parts if p != "None"]
        if len(parts) != 2 or len(non_none) != 1:
            raise ConfigError(
                f"cannot parse state field type {type_str!r} in {where}: "
                "only '<T> | None' unions are supported",
                context={"type": type_str, "where": where},
            )
        return parse_type_string(non_none[0], where=where) | None

    if text in _PRIMITIVES:
        return _PRIMITIVES[text]

    container = _CONTAINER_RE.match(text)
    if container:
        outer, inner = container.group(1), container.group(2)
        args = [parse_type_string(a, where=where) for a in _split_args(inner)]
        if outer == "list":
            if len(args) != 1:
                raise ConfigError(
                    f"list[...] takes exactly one type argument; got {type_str!r}",
                    context={"type": type_str, "where": where},
                )
            return list[args[0]]  # type: ignore[valid-type]
        if outer == "set":
            if len(args) != 1:
                raise ConfigError(
                    f"set[...] takes exactly one type argument; got {type_str!r}",
                    context={"type": type_str, "where": where},
                )
            return set[args[0]]  # type: ignore[valid-type]
        if outer == "dict":
            if len(args) != 2:
                raise ConfigError(
                    f"dict[...] takes exactly two type arguments; got {type_str!r}",
                    context={"type": type_str, "where": where},
                )
            return dict[args[0], args[1]]  # type: ignore[valid-type]
        # tuple[T, ...]
        if inner.endswith(", ..."):
            item = parse_type_string(inner[: -len(", ...")], where=where)
            return tuple[item, ...]  # type: ignore[valid-type]
        return tuple[tuple(args)]  # type: ignore[misc]

    user_model = _USER_MODEL_RE.match(text)
    if user_model:
        module_name = user_model.group("module")
        class_name = user_model.group("cls")
        try:
            module = importlib.import_module(module_name)
        except ImportError as exc:
            raise ConfigError(
                f"state field type {type_str!r} in {where}: module "
                f"{module_name!r} is not importable ({exc})",
                context={"type": type_str, "where": where,
                         "module": module_name},
                cause=exc,
            ) from exc
        cls = getattr(module, class_name, None)
        if cls is None or not (isinstance(cls, type) and issubclass(cls, BaseModel)):
            raise ConfigError(
                f"state field type {type_str!r} in {where}: {class_name!r} in "
                f"{module_name!r} is missing or not a Pydantic BaseModel",
                context={"type": type_str, "where": where},
            )
        return cls

    raise ConfigError(
        f"cannot parse state field type {type_str!r} in {where}; supported: "
        "primitives (str, int, float, bool, bytes, datetime, date, time, "
        "timedelta, Decimal, UUID), list/dict/set/tuple containers, "
        "'<T> | None', FoundryMessage, RetrievedDocument, and "
        "'BaseModel:<module>:<Class>' refs",
        context={"type": type_str, "where": where},
    )


@dataclass(frozen=True)
class AgentStateView:
    """Structural per-agent state scope: TypedDict types + field lists."""

    agent: str
    read: tuple[str, ...]
    write: tuple[str, ...]
    input_type: type
    output_type: type

    def project_input(self, state: dict[str, Any]) -> dict[str, Any]:
        """The agent's view of state — forbidden fields are ABSENT, not
        None-ed. This projection is what the node receives."""
        return {name: state[name] for name in self.read if name in state}

    def validate_writes(self, delta: dict[str, Any]) -> dict[str, Any]:
        """Refuse a state delta touching fields outside the write scope."""
        forbidden = sorted(set(delta) - set(self.write))
        if forbidden:
            raise StateVisibilityError(
                f"agent {self.agent!r} attempted to write field(s) outside "
                f"its declared write scope: {', '.join(forbidden)} "
                f"(write: {', '.join(self.write) or '(none)'})",
                context={"agent": self.agent, "forbidden_fields": forbidden,
                         "write_scope": list(self.write)},
            )
        return delta


@dataclass(frozen=True)
class CompiledState:
    model: type[StateBase]
    reducers: dict[str, Reducer]
    agent_views: dict[str, AgentStateView]
    field_types: dict[str, Any]


def _validate_visibility(
    spec: StateSpec, node_names: list[str], *, where: str
) -> None:
    fields = set(spec.state_schema)
    for node in node_names:
        visibility = spec.visibility.get(node)
        if visibility is None:
            raise StateVisibilityError(
                f"agent {node!r} has no visibility entry in {where}; every "
                "agent in SystemSpec.agents must declare read/write scope",
                context={"agent": node, "where": where,
                         "declared_entries": sorted(spec.visibility)},
            )
        for kind, names in (("read", visibility.read), ("write", visibility.write)):
            unknown = sorted(set(names) - fields)
            if unknown:
                raise StateVisibilityError(
                    f"agent {node!r} {kind}s unknown state field(s) "
                    f"{', '.join(unknown)} in {where} (schema fields: "
                    f"{', '.join(sorted(fields))})",
                    context={"agent": node, "kind": kind,
                             "unknown_fields": unknown,
                             "schema_fields": sorted(fields)},
                )
    orphans = sorted(set(spec.visibility) - set(node_names))
    if orphans:
        raise StateVisibilityError(
            f"visibility entries in {where} reference unknown agent(s): "
            f"{', '.join(orphans)} (system agents: "
            f"{', '.join(sorted(node_names))})",
            context={"unknown_agents": orphans, "system_agents": node_names},
        )


def _build_view(
    agent: str,
    visibility: StateVisibility,
    field_types: dict[str, Any],
) -> AgentStateView:
    prefix = agent.title().replace("_", "")
    # Functional TypedDict with runtime-computed name/fields — legal at
    # runtime, opaque to mypy's static TypedDict analysis.
    input_type: type = TypedDict(  # type: ignore[misc]
        f"{prefix}Input",
        {name: field_types[name] for name in visibility.read},
    )
    output_type: type = TypedDict(  # type: ignore[misc]
        f"{prefix}Output",
        {name: field_types[name] for name in visibility.write},
    )
    return AgentStateView(
        agent=agent,
        read=tuple(visibility.read),
        write=tuple(visibility.write),
        input_type=input_type,
        output_type=output_type,
    )


def compile_state(
    spec: StateSpec,
    node_names: list[str],
    *,
    where: str = "state.yaml",
) -> CompiledState:
    """StateSpec → Pydantic model + reducer map + per-agent TypedDict views
    (docs/22 § How state compiles). All checks are compile-time."""
    field_types: dict[str, Any] = {}
    model_fields: dict[str, Any] = {}
    for name, field_spec in spec.state_schema.items():
        python_type = parse_type_string(field_spec.type, where=where)
        field_types[name] = python_type
        reducer = spec.reducers.get(name, Reducer.LAST_WRITE_WINS)
        annotated: Any = Annotated[python_type, reducer]
        default = _default_for(field_spec, python_type)
        model_fields[name] = (annotated, default)

    unknown_reducers = sorted(set(spec.reducers) - set(spec.state_schema))
    if unknown_reducers:
        raise ConfigError(
            f"reducers in {where} reference unknown field(s): "
            f"{', '.join(unknown_reducers)}",
            context={"unknown_fields": unknown_reducers, "where": where},
        )

    _validate_visibility(spec, node_names, where=where)

    model = create_model("ProjectState", __base__=StateBase, **model_fields)
    reducers = {
        name: spec.reducers.get(name, Reducer.LAST_WRITE_WINS)
        for name in spec.state_schema
    }
    agent_views = {
        agent: _build_view(agent, spec.visibility[agent], field_types)
        for agent in node_names
    }
    return CompiledState(
        model=model,
        reducers=reducers,
        agent_views=agent_views,
        field_types=field_types,
    )


def _default_for(field_spec: FieldSpec, python_type: Any) -> Any:
    if field_spec.default is not None:
        return field_spec.default
    # YAML `default: null` and omitted default are indistinguishable (both
    # None); optional fields get None, everything else is required.
    if _is_optional(python_type):
        return None
    if "default" in field_spec.model_fields_set:
        return None
    return Field(...)


def _is_optional(python_type: Any) -> bool:
    import types
    import typing

    return typing.get_origin(python_type) in (typing.Union, types.UnionType) and (
        type(None) in typing.get_args(python_type)
    )


__all__ = [
    "AgentStateView",
    "CompiledState",
    "compile_state",
    "parse_type_string",
]
