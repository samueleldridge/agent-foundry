"""Read/write version pins in ``system.yaml`` and ``agent.yaml`` (docs/03
§ Phase 5 deliverable 3).

Two properties drive the design:

1. **Surgical edits.** A pin change must produce a one-line (tool /
   connection) or two-line (prompt version + path) diff — comments,
   ordering, and formatting elsewhere in the file are preserved byte-for-
   byte. That is what makes ``git diff HEAD~1`` after a rollback legible
   (docs/52 exit gate). So edits are text-level (indentation-aware block
   scan), NOT a YAML round-trip.

2. **Transactional.** A :class:`PinTransaction` stages any number of edits
   across any number of files; ``apply()`` first validates EVERY edited
   file against its Pydantic schema (SystemSpec / AgentSpec), then writes
   all of them (per-file atomic via temp-file + ``os.replace``). A
   validation failure anywhere writes nothing.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ValidationError

from foundry.config.schemas import AgentSpec, SystemSpec
from foundry.core.errors import ConfigValidationError, PinConflictError

_VERSION_RE = re.compile(r"^v\d+$")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


@dataclass(frozen=True)
class PinChange:
    """One applied (or staged) pin edit, for plans and audit summaries."""

    file: Path
    pointer: str
    """JSON-pointer-ish path, e.g. ``/tools/validate_deltas/version``."""
    old: str
    new: str

    def describe(self) -> str:
        return f"{self.pointer}: {self.old} -> {self.new}"


# --- the indentation-aware scalar replace -------------------------------------------


def _scan_child_key(
    lines: list[str],
    lo: int,
    hi: int,
    parent_indent: int,
    key: str,
    *,
    file: Path,
    pointer: str,
) -> tuple[int, int]:
    """Locate ``key:`` among the DIRECT children of the block spanning
    [lo, hi). Returns (line_index, child_indent)."""
    child_indent: int | None = None
    for i in range(lo, hi):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = _indent_of(lines[i])
        if indent <= parent_indent:
            break
        if child_indent is None:
            child_indent = indent
        if indent != child_indent:
            continue  # grandchild of the block; not a direct child
        if re.match(rf"^{re.escape(key)}\s*:", stripped):
            return i, child_indent
    raise PinConflictError(
        f"could not locate {pointer!r} in {file} — the file must use plain "
        "block-style YAML for pinned sections (edit the pin manually or "
        "normalise the file)",
        context={"file": str(file), "pointer": pointer, "missing_key": key},
    )


def _block_end(lines: list[str], start: int, key_indent: int) -> int:
    """First line after ``start`` that closes the block owned by the key at
    ``start`` (content at indent <= key_indent)."""
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            continue
        if _indent_of(lines[i]) <= key_indent:
            return i
    return len(lines)


def replace_nested_scalar(
    text: str, path: list[str], new_value: str, *, file: Path
) -> tuple[str, str]:
    """Replace the scalar at a nested block-mapping ``path``, preserving
    every other byte (including a trailing comment on the edited line).
    Returns ``(new_text, old_value)``."""
    pointer = "/" + "/".join(path)
    lines = text.splitlines(keepends=True)
    lo, hi, parent_indent = 0, len(lines), -1
    idx = -1
    for segment in path:
        idx, key_indent = _scan_child_key(
            lines, lo, hi, parent_indent, segment, file=file, pointer=pointer
        )
        lo, hi, parent_indent = idx + 1, _block_end(lines, idx, key_indent), key_indent

    line = lines[idx]
    newline = "\n" if line.endswith("\n") else ""
    body = line.rstrip("\n")
    match = re.match(
        rf"^(?P<head>\s*{re.escape(path[-1])}\s*:\s*)"
        r"(?P<value>[^#]*?)(?P<tail>\s*(?:#.*)?)$",
        body,
    )
    old_value = (match.group("value") if match else "").strip()
    if match is None or not old_value:
        raise PinConflictError(
            f"{pointer!r} in {file} does not hold an inline scalar value "
            "(found a nested block or empty value); edit the pin manually",
            context={"file": str(file), "pointer": pointer, "line": body},
        )
    lines[idx] = f"{match.group('head')}{new_value}{match.group('tail')}{newline}"
    return "".join(lines), old_value


# --- raw pin reads (no env resolution required) ---------------------------------------


def _load_yaml_mapping(file: Path) -> dict[str, Any]:
    if not file.is_file():
        raise PinConflictError(
            f"pin file not found: {file}", context={"file": str(file)}
        )
    data = yaml.safe_load(file.read_text())
    if not isinstance(data, dict):
        raise PinConflictError(
            f"{file} is not a YAML mapping", context={"file": str(file)}
        )
    return data


def read_tool_pin(project_dir: Path, tool: str) -> tuple[str, str]:
    """(ref, version) currently pinned for a tool in system.yaml."""
    return _read_binding_pin(project_dir, "tools", tool)


def read_connection_pin(project_dir: Path, connection: str) -> tuple[str, str]:
    """(ref, version) currently pinned for a connection in system.yaml."""
    return _read_binding_pin(project_dir, "connections", connection)


def _read_binding_pin(
    project_dir: Path, section: str, name: str
) -> tuple[str, str]:
    system_file = project_dir / "system.yaml"
    data = _load_yaml_mapping(system_file)
    block = data.get(section) or {}
    entry = block.get(name) if isinstance(block, dict) else None
    if not isinstance(entry, dict):
        known = sorted(block) if isinstance(block, dict) else []
        raise PinConflictError(
            f"{section[:-1]} {name!r} is not bound in {system_file} "
            f"(known: {', '.join(known) or '(none)'})",
            context={"file": str(system_file), "name": name, "known": known},
        )
    return str(entry.get("ref", "")), str(entry.get("version", ""))


def read_prompt_pin(project_dir: Path, agent: str) -> tuple[str, str]:
    """(version, path) currently pinned in the agent's agent.yaml."""
    agent_file = project_dir / "agents" / agent / "agent.yaml"
    data = _load_yaml_mapping(agent_file)
    prompt = data.get("prompt")
    if not isinstance(prompt, dict):
        raise PinConflictError(
            f"agent.yaml at {agent_file} has no `prompt:` block",
            context={"file": str(agent_file)},
        )
    return str(prompt.get("version", "")), str(prompt.get("path", ""))


# --- the transaction --------------------------------------------------------------------


@dataclass
class PinTransaction:
    """Stage pin edits across system.yaml / agent.yaml files, then apply
    them all-or-nothing. Nothing touches disk until :meth:`apply`."""

    project_dir: Path
    _texts: dict[Path, str] = field(default_factory=dict)
    _schemas: dict[Path, type[BaseModel]] = field(default_factory=dict)
    changes: list[PinChange] = field(default_factory=list)

    def _text(self, file: Path, schema: type[BaseModel]) -> str:
        if file not in self._texts:
            if not file.is_file():
                raise PinConflictError(
                    f"pin file not found: {file}", context={"file": str(file)}
                )
            self._texts[file] = file.read_text()
            self._schemas[file] = schema
        return self._texts[file]

    def _edit(
        self,
        file: Path,
        schema: type[BaseModel],
        path: list[str],
        new_value: str,
    ) -> PinChange:
        text = self._text(file, schema)
        new_text, old_value = replace_nested_scalar(
            text, path, new_value, file=file
        )
        self._texts[file] = new_text
        change = PinChange(
            file=file,
            pointer="/" + "/".join(path),
            old=old_value,
            new=new_value,
        )
        self.changes.append(change)
        return change

    # -- staging helpers --

    def set_tool_version(self, tool: str, new_version: str) -> PinChange:
        _require_version(new_version)
        return self._edit(
            self.project_dir / "system.yaml",
            SystemSpec,
            ["tools", tool, "version"],
            new_version,
        )

    def set_connection_version(
        self, connection: str, new_version: str
    ) -> PinChange:
        _require_version(new_version)
        return self._edit(
            self.project_dir / "system.yaml",
            SystemSpec,
            ["connections", connection, "version"],
            new_version,
        )

    def set_prompt_version(self, agent: str, new_version: str) -> list[PinChange]:
        """Prompt pins are TWO coupled fields (PromptRef.version + .path);
        both are staged together so the file stays self-consistent."""
        _require_version(new_version)
        agent_file = self.project_dir / "agents" / agent / "agent.yaml"
        version_change = self._edit(
            agent_file, AgentSpec, ["prompt", "version"], new_version
        )
        _, old_path = read_prompt_pin(self.project_dir, agent)
        new_path = re.sub(r"v\d+\.md$", f"{new_version}.md", old_path)
        if new_path == old_path and not old_path.endswith(f"{new_version}.md"):
            raise PinConflictError(
                f"prompt path {old_path!r} in {agent_file} does not follow "
                "the v<N>.md convention; edit the pin manually",
                context={"file": str(agent_file), "path": old_path},
            )
        path_change = self._edit(
            agent_file, AgentSpec, ["prompt", "path"], new_path
        )
        return [version_change, path_change]

    # -- apply --

    def files(self) -> list[Path]:
        return sorted(self._texts)

    def apply(self) -> list[Path]:
        """Validate every staged file against its schema, THEN write all.
        A validation failure anywhere leaves every file untouched."""
        if not self.changes:
            raise PinConflictError(
                "pin transaction has no staged edits",
                context={"project_dir": str(self.project_dir)},
            )
        for file, text in self._texts.items():
            schema = self._schemas[file]
            try:
                schema.model_validate(yaml.safe_load(text))
            except (yaml.YAMLError, ValidationError) as exc:
                raise ConfigValidationError(
                    f"staged pin edit would make {file} invalid against "
                    f"{schema.__name__}; nothing was written: {exc}",
                    context={"file": str(file), "schema": schema.__name__},
                    cause=exc,
                ) from exc
        written: list[Path] = []
        for file, text in self._texts.items():
            tmp = file.with_name(file.name + ".foundry-pin-tmp")
            tmp.write_text(text)
            os.replace(tmp, file)
            written.append(file)
        return written


def _require_version(version: str) -> None:
    if not _VERSION_RE.match(version):
        raise PinConflictError(
            f"invalid version {version!r} (expected v<N>)",
            context={"version": version},
        )


__all__ = [
    "PinChange",
    "PinTransaction",
    "read_connection_pin",
    "read_prompt_pin",
    "read_tool_pin",
    "replace_nested_scalar",
]
