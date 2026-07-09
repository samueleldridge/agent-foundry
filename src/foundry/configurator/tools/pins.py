"""Pinning meta-tool: ``pin_version`` (docs/61 § pin_version).

Wraps :class:`foundry.versioning.pins.PinTransaction` — the same surgical,
validate-before-write pin editor the CLI rollback uses. ``key_path`` is
validated against the FIXED set of pin-able locations; the target version
must exist on disk before the pin moves (fail loudly, adapt in the next
turn).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from foundry.configurator.tools.context import (
    MetaToolContext,
    check_write_path,
)
from foundry.core.errors import ConfigError, RefResolutionError
from foundry.core.tool import RunContext
from foundry.versioning.artifacts import list_prompt_versions, prompts_dir
from foundry.versioning.pins import PinTransaction, read_prompt_pin
from foundry.versioning.refs import parse_artifact_ref

_TOOL_KEY_RE = re.compile(r"^tools\.([a-z][a-z0-9_-]{0,63})\.version$")
_CONNECTION_KEY_RE = re.compile(
    r"^connections\.([a-z][a-z0-9_-]{0,63})\.version$"
)
_PROMPT_KEY = "prompt.version"
_AGENT_FILE_RE = re.compile(r"agents/([a-z][a-z0-9_-]{0,63})/agent\.yaml$")


class PinVersionIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    """'system.yaml' (tool/connection pins) or
    'agents/<agent>/agent.yaml' (prompt pins); project-relative or full."""
    key_path: str
    """One of: 'tools.<name>.version', 'connections.<name>.version',
    'prompt.version'."""
    new_version: str = Field(pattern=r"^v\d+$")


class PinResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    key_path: str
    old_version: str
    new_version: str
    related_field_updates: dict[str, str] = Field(default_factory=dict)


def make_pin_version(
    mctx: MetaToolContext,
) -> Callable[[PinVersionIn, RunContext], Awaitable[PinResult]]:
    async def handle(inputs: PinVersionIn, ctx: RunContext) -> PinResult:
        raw = inputs.file
        if not raw.startswith(("/", "projects/")):
            raw = str(mctx.project_dir / raw)
        path = check_write_path(mctx, ctx.session, raw, tool="pin_version")

        txn = PinTransaction(mctx.project_dir)
        related: dict[str, str] = {}

        tool_match = _TOOL_KEY_RE.match(inputs.key_path)
        connection_match = _CONNECTION_KEY_RE.match(inputs.key_path)
        if tool_match or connection_match:
            if path.name != "system.yaml":
                raise ConfigError(
                    f"pin_version: key_path {inputs.key_path!r} lives in "
                    f"system.yaml, not {path.name}",
                    context={"file": str(path), "key_path": inputs.key_path},
                )
            if tool_match:
                name = tool_match.group(1)
                _require_binding_version(
                    mctx, "tools", name, inputs.new_version
                )
                change = txn.set_tool_version(name, inputs.new_version)
            else:
                assert connection_match is not None
                name = connection_match.group(1)
                _require_binding_version(
                    mctx, "connections", name, inputs.new_version
                )
                change = txn.set_connection_version(name, inputs.new_version)
            old = change.old
        elif inputs.key_path == _PROMPT_KEY:
            agent_match = _AGENT_FILE_RE.search(path.as_posix())
            if agent_match is None:
                raise ConfigError(
                    "pin_version: 'prompt.version' pins live in "
                    "agents/<agent>/agent.yaml",
                    context={"file": str(path), "key_path": inputs.key_path},
                )
            agent = agent_match.group(1)
            available = list_prompt_versions(prompts_dir(mctx.project_dir, agent))
            if inputs.new_version not in available:
                raise RefResolutionError(
                    f"pin_version: prompt {inputs.new_version} does not "
                    f"exist for agent {agent!r} (available: "
                    f"{', '.join(available) or 'none'})",
                    context={"agent": agent, "available": available},
                )
            old, _old_path = read_prompt_pin(mctx.project_dir, agent)
            changes = txn.set_prompt_version(agent, inputs.new_version)
            related = {
                "prompt.path": changes[1].new,
            }
        else:
            raise ConfigError(
                f"pin_version: unknown pin key_path {inputs.key_path!r}; "
                "pin-able locations: tools.<name>.version, "
                "connections.<name>.version, prompt.version",
                context={"key_path": inputs.key_path},
            )
        txn.apply()
        return PinResult(
            file=str(path),
            key_path=inputs.key_path,
            old_version=old,
            new_version=inputs.new_version,
            related_field_updates=related,
        )

    return handle


def _require_binding_version(
    mctx: MetaToolContext, section: str, name: str, version: str
) -> None:
    """The pinned-to version must exist on disk (docs/61 error semantics)."""
    system = mctx.project_dir / "system.yaml"
    data = yaml.safe_load(system.read_text()) if system.is_file() else {}
    bindings = data.get(section) if isinstance(data, dict) else None
    binding = bindings.get(name) if isinstance(bindings, dict) else None
    if not isinstance(binding, dict):
        raise ConfigError(
            f"pin_version: {section[:-1]} {name!r} is not bound in "
            f"{system}",
            context={"name": name, "section": section},
        )
    kind: Literal["tool", "connection"] = (
        "tool" if section == "tools" else "connection"
    )
    ref = parse_artifact_ref(
        str(binding.get("ref", "")), default_kind=kind, version=version
    )
    ref.resolve_path(mctx.roots())  # raises RefResolutionError when missing

    return


__all__ = ["PinResult", "PinVersionIn", "make_pin_version"]
