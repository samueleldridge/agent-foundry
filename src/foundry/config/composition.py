"""Composition: ``extends`` (one-deep) + ``${ENV:NAME[:default]}`` interpolation.

Deliberately small (docs/12 § Composition): no recursive includes, no Jinja.
A config file must be readable top to bottom with at most one ``extends`` hop.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from foundry.core.errors import ConfigLoadError

_ENV_PATTERN = re.compile(
    r"^\$\{ENV:(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?::(?P<default>[^}]*))?\}$"
)


def apply_extends(data: Any, file: Path) -> Any:
    """Resolve a single ``extends`` hop: load the base file into a plain dict,
    shallow-merge the current file's keys on top. List fields are replaced,
    not extended.
    """
    if not isinstance(data, dict) or "extends" not in data:
        return data

    target = data["extends"]
    if not isinstance(target, str):
        raise ConfigLoadError(
            "extends must be a relative path string",
            context={"file": str(file), "received": repr(target)},
        )
    base_path = (file.parent / target).resolve()
    if not base_path.exists():
        raise ConfigLoadError(
            f"extends target not found: {base_path}",
            context={"file": str(file), "extends": target,
                     "resolved": str(base_path)},
        )
    try:
        base_raw = yaml.safe_load(base_path.read_text())
    except yaml.YAMLError as exc:
        raise ConfigLoadError(
            f"extends target is not valid YAML: {base_path}",
            context={"file": str(file), "extends": target},
            cause=exc,
        ) from exc
    if not isinstance(base_raw, dict):
        raise ConfigLoadError(
            f"extends target must be a mapping: {base_path}",
            context={"file": str(file), "extends": target},
        )
    if "extends" in base_raw:
        raise ConfigLoadError(
            "extends is one-deep only; the base file may not extend another",
            context={"file": str(file), "extends": target,
                     "base_extends": base_raw["extends"]},
        )

    merged = dict(base_raw)
    for key, value in data.items():
        if key == "extends":
            continue
        merged[key] = value
    return merged


def interpolate_env(data: Any, file: Path) -> Any:
    """Substitute ``${ENV:NAME}`` / ``${ENV:NAME:default}`` scalars in place.

    Scalar-only: whole string values, never keys or partial substrings.
    Missing env var with no default raises ``ConfigLoadError``.
    """
    if isinstance(data, dict):
        return {k: interpolate_env(v, file) for k, v in data.items()}
    if isinstance(data, list):
        return [interpolate_env(v, file) for v in data]
    if isinstance(data, str):
        match = _ENV_PATTERN.match(data)
        if match is None:
            return data
        name = match.group("name")
        default = match.group("default")
        value = os.environ.get(name)
        if value is not None:
            return value
        if default is not None:
            return default
        raise ConfigLoadError(
            f"environment variable {name!r} is not set and no default was given",
            context={"file": str(file), "env_var": name, "placeholder": data},
        )
    return data


__all__ = ["apply_extends", "interpolate_env"]
