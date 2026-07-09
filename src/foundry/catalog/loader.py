"""Catalog index loading, version discovery, and versioned-artifact loading.

This module owns the on-disk contract for versioned artifacts:

- the 5-file tool shape  — tool.yaml, handler.py, schemas.py, eval.yaml,
  README.md (docs/20 § The 5-file shape);
- the 5-file connection shape — connection.yaml, auth.py, schemas.py,
  health.yaml, README.md (docs/23 § build_connection);
- the 5-file retriever shape — retriever.yaml, factory.py, schemas.py,
  health.yaml, README.md (docs/25 § Catalog template details; reranker
  artifacts share it with ``kind: reranker``);
- ``versions.json`` metadata per artifact (docs/12 § VersionsMetadata);
- ``index.yaml`` per catalog root.

Catalog promotion (writing INTO the catalog) is Phase 5; everything here is
read-only.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

from pydantic import BaseModel, ValidationError

from foundry.catalog.schemas import (
    CatalogEntry,
    CatalogIndex,
    VersionsMetadata,
)
from foundry.config import (
    ArtifactRef,
    ConnectionSpec,
    FoundryRoots,
    RetrieverSpec,
    ToolSpec,
    list_versions,
    load_config_model,
    load_connection_spec,
    load_retriever_spec,
    load_tool_spec,
)
from foundry.core.errors import (
    CompileError,
    ConfigLoadError,
    ConfigValidationError,
)
from foundry.core.tool import ToolHandler, validate_handler_signature

_TOOL_FILES = ("tool.yaml", "handler.py", "schemas.py", "eval.yaml", "README.md")
_CONNECTION_FILES = (
    "connection.yaml",
    "auth.py",
    "schemas.py",
    "health.yaml",
    "README.md",
)
_RETRIEVER_FILES = (
    "retriever.yaml",
    "factory.py",
    "schemas.py",
    "health.yaml",
    "README.md",
)


# --- index + versions metadata -------------------------------------------------


def load_catalog_index(root: Path) -> CatalogIndex:
    """Load <root>/index.yaml. Missing file → empty index (a catalog root
    without an index is legal; discovery still works from the filesystem)."""
    index_path = root / "index.yaml"
    if not index_path.exists():
        return CatalogIndex()
    return load_config_model(CatalogIndex, index_path)


def load_versions_metadata(path: Path) -> VersionsMetadata:
    """Load a versions.json with the same error ergonomics as YAML configs."""
    if not path.exists():
        raise ConfigLoadError(
            f"versions metadata not found: {path}", context={"file": str(path)}
        )
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigLoadError(
            f"invalid JSON in {path} (line {exc.lineno}, column {exc.colno}): "
            f"{exc.msg}",
            context={"file": str(path), "line": exc.lineno, "column": exc.colno},
            cause=exc,
        ) from exc
    try:
        return VersionsMetadata.model_validate(data)
    except ValidationError as exc:
        first = exc.errors()[0]
        pointer = "/" + "/".join(str(p) for p in first["loc"])
        raise ConfigValidationError(
            f"Invalid VersionsMetadata\n  file: {path}\n  pointer: {pointer}\n"
            f"  expected: {first['msg']}",
            context={"file": str(path), "pointer": pointer,
                     "expected": first["msg"]},
            cause=exc,
        ) from exc


def catalog_entries(roots: FoundryRoots) -> list[CatalogEntry]:
    """Every tool + connection + retriever visible across the catalog roots,
    with the versions each has on disk."""
    entries: list[CatalogEntry] = []
    seen: set[tuple[str, str]] = set()
    for root in roots.catalog_roots:
        for kind, subdir in (
            ("tool", "tools"),
            ("connection", "connections"),
            ("retriever", "retrievers"),
        ):
            base = root / subdir
            if not base.is_dir():
                continue
            for artifact_dir in sorted(p for p in base.iterdir() if p.is_dir()):
                key = (kind, artifact_dir.name)
                if key in seen:  # earlier root shadows
                    continue
                seen.add(key)
                latest_file = artifact_dir / "LATEST"
                entries.append(
                    CatalogEntry(
                        name=artifact_dir.name,
                        kind=kind,  # type: ignore[arg-type]
                        versions=list_versions(artifact_dir),
                        latest=(
                            latest_file.read_text().strip()
                            if latest_file.exists()
                            else None
                        ),
                        root=str(root),
                    )
                )
    return entries


# --- python-module loading -----------------------------------------------------


def _import_module(path: Path, *, role: str) -> ModuleType:
    # Module identity is the FILE, not the role: input_schema, output_schema,
    # and the handler's `from schemas import ...` must all resolve to the same
    # module object, or isinstance-based output validation would spuriously
    # fail on classes loaded twice.
    digest = hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]
    module_name = f"_foundry_artifact_{digest}"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise CompileError(
            f"could not import {role} module: {path}",
            context={"file": str(path), "role": role},
        )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[module_name]
        raise CompileError(
            f"{role} module {path} failed to import: "
            f"{type(exc).__name__}: {exc}",
            context={"file": str(path), "role": role,
                     "cause_type": type(exc).__name__},
            cause=exc if isinstance(exc, Exception) else None,
        ) from exc
    return module


@contextmanager
def _sibling_schemas_alias(version_dir: Path) -> Iterator[None]:
    """Expose the version dir's schemas.py as importable ``schemas`` while a
    sibling module (handler.py) executes.

    Version directories are not packages (no __init__.py — the 5-file shape
    is flat), so a handler cannot use relative imports. Instead it writes
    ``from schemas import QueryIn, QueryOut`` and the loader satisfies that
    import by aliasing the already-loaded sibling module for the duration of
    the exec. Documented in the Phase 2a handoff as a deviation from the
    docs/20 sketch (which showed ``from .schemas import ...``).
    """
    module = _import_module(version_dir / "schemas.py", role="schemas")
    previous = sys.modules.get("schemas")
    sys.modules["schemas"] = module
    try:
        yield
    finally:
        if previous is None:
            sys.modules.pop("schemas", None)
        else:
            sys.modules["schemas"] = previous


def _resolve_symbol(version_dir: Path, ref: str, *, role: str) -> Any:
    """Resolve a '<file>.py::<symbol>' ref relative to a version directory."""
    if "::" not in ref:
        raise CompileError(
            f"{role} ref must look like 'file.py::symbol'; got {ref!r}",
            context={"version_dir": str(version_dir), "ref": ref},
        )
    file_part, symbol = ref.split("::", 1)
    module_path = version_dir / file_part
    if not module_path.exists():
        raise CompileError(
            f"{role} module not found: {module_path} (referenced as {ref!r})",
            context={"version_dir": str(version_dir), "ref": ref},
        )
    module = _import_module(module_path, role=role)
    value = getattr(module, symbol, None)
    if value is None:
        raise CompileError(
            f"{role} symbol {symbol!r} not found in {module_path}",
            context={"file": str(module_path), "symbol": symbol},
        )
    return value


def _resolve_model(version_dir: Path, ref: str, *, role: str) -> type[BaseModel]:
    value = _resolve_symbol(version_dir, ref, role=role)
    if not (isinstance(value, type) and issubclass(value, BaseModel)):
        raise CompileError(
            f"{role} {ref!r} in {version_dir} is not a Pydantic BaseModel "
            "subclass",
            context={"version_dir": str(version_dir), "ref": ref},
        )
    return value


def _enforce_file_shape(
    version_dir: Path, required: tuple[str, ...], *, kind: str
) -> None:
    missing = [name for name in required if not (version_dir / name).exists()]
    if missing:
        raise CompileError(
            f"{kind} version at {version_dir} is missing required file(s): "
            f"{', '.join(missing)} (the {len(required)}-file shape is "
            "enforced; see docs/20 / docs/23)",
            context={"version_dir": str(version_dir), "missing_files": missing},
        )


# --- versioned tool loading ------------------------------------------------------


@dataclass(frozen=True)
class LoadedToolVersion:
    ref: ArtifactRef
    directory: Path
    spec: ToolSpec
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: ToolHandler


def load_tool_version(ref: ArtifactRef, roots: FoundryRoots) -> LoadedToolVersion:
    """Resolve + load one pinned tool version (5-file shape enforced)."""
    version_dir = ref.resolve_path(roots)
    _enforce_file_shape(version_dir, _TOOL_FILES, kind="tool")
    spec = load_tool_spec(version_dir / "tool.yaml")
    if spec.name != ref.name or spec.version != ref.version:
        raise CompileError(
            f"tool.yaml at {version_dir} declares "
            f"{spec.name!r}@{spec.version} but the directory is "
            f"{ref.name!r}@{ref.version}; version directories are immutable "
            "and self-consistent",
            context={
                "version_dir": str(version_dir),
                "declared": f"{spec.name}@{spec.version}",
                "expected": f"{ref.name}@{ref.version}",
            },
        )
    input_model = _resolve_model(version_dir, spec.input_schema, role="input_schema")
    output_model = _resolve_model(
        version_dir, spec.output_schema, role="output_schema"
    )
    with _sibling_schemas_alias(version_dir):
        handler_obj = _resolve_symbol(version_dir, spec.handler, role="handler")
    handler = validate_handler_signature(
        handler_obj, where=f"{version_dir / spec.handler.split('::')[0]}"
    )
    return LoadedToolVersion(
        ref=ref,
        directory=version_dir,
        spec=spec,
        input_model=input_model,
        output_model=output_model,
        handler=handler,
    )


def load_tool_contract(
    version_dir: Path,
) -> tuple[ToolSpec, type[BaseModel], type[BaseModel]]:
    """A tool version's CONTRACT only — spec + input/output models — without
    importing the handler. Used by Phase 5 schema-compatibility checks
    (rollback pre-flight + catalog promotion semver detection, docs/50)."""
    spec = load_tool_spec(version_dir / "tool.yaml")
    input_model = _resolve_model(version_dir, spec.input_schema, role="input_schema")
    output_model = _resolve_model(
        version_dir, spec.output_schema, role="output_schema"
    )
    return spec, input_model, output_model


def load_connection_contract(
    version_dir: Path,
) -> tuple[ConnectionSpec, type[BaseModel]]:
    """A connection version's contract — spec + config model — without
    importing the auth factory. Same consumers as ``load_tool_contract``."""
    spec = load_connection_spec(version_dir / "connection.yaml")
    config_model = _resolve_model(
        version_dir, spec.config_schema, role="config_schema"
    )
    return spec, config_model


# --- versioned connection loading ------------------------------------------------


@dataclass(frozen=True)
class LoadedConnectionVersion:
    ref: ArtifactRef
    directory: Path
    spec: ConnectionSpec
    config_model: type[BaseModel]
    factory: Any
    """The auth.py::build_connection callable (ConnectionFactory protocol)."""
    health_check_path: Path | None


def load_connection_version(
    ref: ArtifactRef, roots: FoundryRoots
) -> LoadedConnectionVersion:
    """Resolve + load one pinned connection version — the SAME resolution
    code path as tools (ArtifactRef.resolve_path), per the exit gate."""
    version_dir = ref.resolve_path(roots)
    _enforce_file_shape(version_dir, _CONNECTION_FILES, kind="connection")
    spec = load_connection_spec(version_dir / "connection.yaml")
    if spec.name != ref.name or spec.version != ref.version:
        raise CompileError(
            f"connection.yaml at {version_dir} declares "
            f"{spec.name!r}@{spec.version} but the directory is "
            f"{ref.name!r}@{ref.version}",
            context={
                "version_dir": str(version_dir),
                "declared": f"{spec.name}@{spec.version}",
                "expected": f"{ref.name}@{ref.version}",
            },
        )
    config_model = _resolve_model(version_dir, spec.config_schema, role="config_schema")
    factory = _resolve_symbol(version_dir, spec.factory, role="factory")
    if not callable(factory):
        raise CompileError(
            f"connection factory {spec.factory!r} in {version_dir} is not "
            "callable",
            context={"version_dir": str(version_dir), "factory": spec.factory},
        )
    health_path: Path | None = None
    if spec.health_check is not None:
        health_path = version_dir / spec.health_check
    return LoadedConnectionVersion(
        ref=ref,
        directory=version_dir,
        spec=spec,
        config_model=config_model,
        factory=factory,
        health_check_path=health_path,
    )


# --- versioned retriever loading ---------------------------------------------------


@dataclass(frozen=True)
class LoadedRetrieverVersion:
    ref: ArtifactRef
    directory: Path
    spec: RetrieverSpec
    config_model: type[BaseModel]
    factory: Any
    """The factory.py callable (async, returns a Retriever or Reranker)."""
    health_check_path: Path | None


def load_retriever_version(
    ref: ArtifactRef, roots: FoundryRoots
) -> LoadedRetrieverVersion:
    """Resolve + load one pinned retriever/reranker version — the SAME
    resolution code path as tools and connections (ArtifactRef.resolve_path)."""
    version_dir = ref.resolve_path(roots)
    _enforce_file_shape(version_dir, _RETRIEVER_FILES, kind="retriever")
    spec = load_retriever_spec(version_dir / "retriever.yaml")
    if spec.name != ref.name or spec.version != ref.version:
        raise CompileError(
            f"retriever.yaml at {version_dir} declares "
            f"{spec.name!r}@{spec.version} but the directory is "
            f"{ref.name!r}@{ref.version}",
            context={
                "version_dir": str(version_dir),
                "declared": f"{spec.name}@{spec.version}",
                "expected": f"{ref.name}@{ref.version}",
            },
        )
    config_model = _resolve_model(version_dir, spec.config_schema, role="config_schema")
    with _sibling_schemas_alias(version_dir):
        factory = _resolve_symbol(version_dir, spec.factory, role="factory")
    if not callable(factory):
        raise CompileError(
            f"retriever factory {spec.factory!r} in {version_dir} is not "
            "callable",
            context={"version_dir": str(version_dir), "factory": spec.factory},
        )
    health_path: Path | None = None
    if spec.health_check is not None:
        health_path = version_dir / spec.health_check
    return LoadedRetrieverVersion(
        ref=ref,
        directory=version_dir,
        spec=spec,
        config_model=config_model,
        factory=factory,
        health_check_path=health_path,
    )


__all__ = [
    "LoadedConnectionVersion",
    "LoadedRetrieverVersion",
    "LoadedToolVersion",
    "catalog_entries",
    "load_catalog_index",
    "load_connection_contract",
    "load_connection_version",
    "load_retriever_version",
    "load_tool_contract",
    "load_tool_version",
    "load_versions_metadata",
]
