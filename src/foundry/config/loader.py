"""YAML → Pydantic with structured error reporting (docs/12 § The loader).

Pipeline per file:

    text → yaml (SafeLoader, position-tracked) → extends → env interpolation
         → secret-literal scan → Pydantic validation → typed instance

Errors at any stage raise ``ConfigLoadError`` / ``ConfigValidationError``
carrying file, JSON pointer, line/column, received vs expected, and a
best-effort did-you-mean hint (difflib close-match against field names or
enum members).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from foundry.config.composition import apply_extends, interpolate_env
from foundry.config.schemas import (
    AgentSpec,
    ConnectionSpec,
    EvalSpec,
    FunctionNodeSpec,
    RetrieverSpec,
    StateSpec,
    SystemSpec,
    ToolSpec,
)
from foundry.config.secrets import scan_for_secret_literals
from foundry.core.errors import ConfigLoadError, ConfigValidationError

# --- position-tracked YAML parse ----------------------------------------------


def _walk_node(
    node: yaml.Node, pointer: str, positions: dict[str, tuple[int, int]]
) -> None:
    positions[pointer] = (node.start_mark.line + 1, node.start_mark.column + 1)
    if isinstance(node, yaml.MappingNode):
        for key_node, value_node in node.value:
            key = str(getattr(key_node, "value", key_node))
            _walk_node(value_node, f"{pointer}/{key}", positions)
    elif isinstance(node, yaml.SequenceNode):
        for index, item in enumerate(node.value):
            _walk_node(item, f"{pointer}/{index}", positions)


def _parse_yaml(text: str, file: Path) -> tuple[Any, dict[str, tuple[int, int]]]:
    try:
        root = yaml.compose(text, Loader=yaml.SafeLoader)
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        context: dict[str, Any] = {"file": str(file)}
        mark = getattr(exc, "problem_mark", None)
        if mark is not None:
            context["line"] = mark.line + 1
            context["column"] = mark.column + 1
        raise ConfigLoadError(
            f"invalid YAML in {file}"
            + (f" (line {mark.line + 1}, column {mark.column + 1})" if mark else "")
            + f": {exc}",
            context=context,
            cause=exc,
        ) from exc
    positions: dict[str, tuple[int, int]] = {}
    if root is not None:
        _walk_node(root, "", positions)
    return data, positions


# --- hint generation ---------------------------------------------------------------


def _close_match(received: str, candidates: list[str]) -> str | None:
    matches = difflib.get_close_matches(received, candidates, n=1, cutoff=0.6)
    return matches[0] if matches else None


def _fields_at(model_cls: type[BaseModel], loc: tuple[Any, ...]) -> list[str]:
    """Best-effort: field names of the (sub)model addressed by ``loc[:-1]``.
    Walks Pydantic model_fields through dicts/lists/unions; gives up (returns
    []) when the path can't be followed statically."""
    current: Any = model_cls
    for part in loc[:-1]:
        current = _step_into(current, part)
        if current is None:
            return []
    if isinstance(current, type) and issubclass(current, BaseModel):
        out = []
        for name, f in current.model_fields.items():
            out.append(f.alias or name)
        return out
    return []


def _step_into(current: Any, part: Any) -> Any:
    import types
    import typing

    # unwrap Annotated / unions to their BaseModel members lazily
    if isinstance(part, str) and isinstance(current, type) and issubclass(
        current, BaseModel
    ):
        f = current.model_fields.get(part)
        if f is None:
            # part might be an alias or a union discriminator tag; try aliases
            for candidate in current.model_fields.values():
                if candidate.alias == part:
                    f = candidate
                    break
        if f is None:
            return None
        return f.annotation
    origin = typing.get_origin(current)
    if origin in (dict,):
        return typing.get_args(current)[1] if typing.get_args(current) else None
    if origin in (list, tuple):
        return typing.get_args(current)[0] if typing.get_args(current) else None
    if origin in (typing.Union, types.UnionType):
        # discriminated union: `part` is usually the tag (e.g. 'single');
        # try to match a member whose discriminator literal equals part
        for member in typing.get_args(current):
            member_origin = typing.get_origin(member)
            if member_origin is typing.Annotated:
                member = typing.get_args(member)[0]
            if isinstance(member, type) and issubclass(member, BaseModel):
                type_field = member.model_fields.get("type")
                if type_field is not None and part in str(type_field.default):
                    return member
        return None
    if origin is typing.Annotated:
        return _step_into(typing.get_args(current)[0], part)
    return None


# Preferred error kinds when a validation produces several errors: the
# extra_forbidden / tag errors carry the most actionable did-you-mean hints.
_ERROR_PRIORITY = {"extra_forbidden": 0, "union_tag_invalid": 1, "literal_error": 2, "enum": 2}


def _quoted_values(raw: str) -> list[str]:
    parts: list[str] = []
    for chunk in raw.replace(" or ", ",").split(","):
        cleaned = chunk.strip().strip("'\"")
        if cleaned:
            parts.append(cleaned)
    return parts


def _format_error(
    model_cls: type[BaseModel],
    exc: ValidationError,
    file: Path,
    positions: dict[str, tuple[int, int]],
) -> ConfigValidationError:
    all_errors = exc.errors()
    first = min(
        all_errors, key=lambda e: _ERROR_PRIORITY.get(e["type"], 9)
    )
    loc = first["loc"]
    pointer = "/" + "/".join(str(p) for p in loc)
    line_col = positions.get(pointer)
    if line_col is None:  # fall back to the parent node's position
        parent = "/" + "/".join(str(p) for p in loc[:-1]) if len(loc) > 1 else ""
        line_col = positions.get(parent)

    received = first.get("input")
    ctx = first.get("ctx", {})
    expected = ctx.get("expected") or first["msg"]

    hint: str | None = None
    if first["type"] == "extra_forbidden":
        candidates = _fields_at(model_cls, loc)
        match = _close_match(str(loc[-1]), candidates)
        if match is not None:
            hint = f'did you mean "{match}"?'
    elif first["type"] == "union_tag_invalid":
        tag = str(ctx.get("tag", ""))
        match = _close_match(tag, _quoted_values(str(ctx.get("expected_tags", ""))))
        if match is not None:
            hint = f'did you mean "{match}"?'
    elif first["type"] in ("literal_error", "enum") and isinstance(received, str):
        match = _close_match(received, _quoted_values(str(ctx.get("expected", ""))))
        if match is not None:
            hint = f'did you mean "{match}"?'

    lines = [
        f"Invalid {model_cls.__name__}",
        f"  file: {file}",
        f"  pointer: {pointer}",
    ]
    if line_col is not None:
        lines.append(f"  line: {line_col[0]}, column: {line_col[1]}")
    if received is not None and first["type"] != "missing":
        lines.append(f"  received: {received!r}")
    lines.append(f"  expected: {expected}")
    if hint:
        lines.append(f"  hint: {hint}")
    if len(exc.errors()) > 1:
        lines.append(f"  (+ {len(exc.errors()) - 1} more validation error(s))")

    return ConfigValidationError(
        "\n".join(lines),
        context={
            "file": str(file),
            "pointer": pointer,
            "line": line_col[0] if line_col else None,
            "column": line_col[1] if line_col else None,
            "received": repr(received),
            "expected": str(expected),
            "hint": hint,
            "error_count": len(exc.errors()),
        },
        cause=exc,
    )


# --- generic load pipeline ------------------------------------------------------


def _load[ModelT: BaseModel](model_cls: type[ModelT], path: Path) -> ModelT:
    path = path.resolve()
    if not path.exists():
        raise ConfigLoadError(
            f"config file not found: {path}", context={"file": str(path)}
        )
    text = path.read_text()
    data, positions = _parse_yaml(text, path)
    if not isinstance(data, dict):
        raise ConfigLoadError(
            f"top-level YAML in {path} must be a mapping (got "
            f"{type(data).__name__}); check for a syntax error",
            context={"file": str(path), "received_type": type(data).__name__},
        )
    data = apply_extends(data, path)
    data = interpolate_env(data, path)
    scan_for_secret_literals(data, path, text, positions)
    try:
        return model_cls.model_validate(data)
    except ValidationError as exc:
        raise _format_error(model_cls, exc, path, positions) from exc


def load_config_model[ModelT: BaseModel](model_cls: type[ModelT], path: Path) -> ModelT:
    """Public generic entry point for consumers with their own schemas
    (e.g. foundry.catalog's CatalogIndex). Same pipeline + error reporting
    as the typed load_* helpers."""
    return _load(model_cls, path)


def load_system_spec(path: Path) -> SystemSpec:
    return _load(SystemSpec, path)


def load_state_spec(path: Path) -> StateSpec:
    return _load(StateSpec, path)


def load_agent_spec(path: Path) -> AgentSpec:
    return _load(AgentSpec, path)


def load_function_node_spec(path: Path) -> FunctionNodeSpec:
    return _load(FunctionNodeSpec, path)


def load_tool_spec(path: Path) -> ToolSpec:
    return _load(ToolSpec, path)


def load_connection_spec(path: Path) -> ConnectionSpec:
    return _load(ConnectionSpec, path)


def load_retriever_spec(path: Path) -> RetrieverSpec:
    return _load(RetrieverSpec, path)


def load_eval_spec(path: Path) -> EvalSpec:
    return _load(EvalSpec, path)


# --- whole-project loading --------------------------------------------------------


@dataclass(frozen=True)
class LoadedAgent:
    spec: AgentSpec
    directory: Path
    prompt_text: str


@dataclass(frozen=True)
class LoadedProject:
    directory: Path
    system: SystemSpec
    state: StateSpec
    agents: dict[str, LoadedAgent] = field(default_factory=dict)


def load_project(project_dir: Path) -> LoadedProject:
    """Load a whole project: system.yaml + state spec + every agent's
    agent.yaml and pinned prompt file."""
    project_dir = project_dir.resolve()
    if not project_dir.is_dir():
        raise ConfigLoadError(
            f"project directory not found: {project_dir}",
            context={"project_dir": str(project_dir)},
        )
    system = load_system_spec(project_dir / "system.yaml")
    state = load_state_spec(project_dir / system.state)

    agents: dict[str, LoadedAgent] = {}
    for agent_name in system.agents:
        agent_dir = project_dir / "agents" / agent_name
        spec = load_agent_spec(agent_dir / "agent.yaml")
        prompt_path = agent_dir / spec.prompt.path
        if not prompt_path.exists():
            raise ConfigLoadError(
                f"prompt file not found: {prompt_path} "
                f"(pinned by {agent_dir / 'agent.yaml'} → prompt.path)",
                context={
                    "file": str(agent_dir / "agent.yaml"),
                    "pointer": "/prompt/path",
                    "prompt_path": str(prompt_path),
                },
            )
        agents[agent_name] = LoadedAgent(
            spec=spec, directory=agent_dir, prompt_text=prompt_path.read_text()
        )
    return LoadedProject(
        directory=project_dir, system=system, state=state, agents=agents
    )


__all__ = [
    "LoadedAgent",
    "LoadedProject",
    "load_agent_spec",
    "load_config_model",
    "load_connection_spec",
    "load_eval_spec",
    "load_function_node_spec",
    "load_project",
    "load_retriever_spec",
    "load_state_spec",
    "load_system_spec",
    "load_tool_spec",
]
