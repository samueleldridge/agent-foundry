"""Filesystem meta-tools: ``read_file`` / ``write_file`` (docs/61 § Filesystem).

Both are ordinary foundry tools (Pydantic in/out, dispatched through the
meta-agent's ``ToolRegistry``); the sandbox checks in
:mod:`foundry.configurator.tools.context` run BEFORE any I/O.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict

from foundry.configurator.tools.context import (
    MetaToolContext,
    check_read_path,
    check_write_path,
)
from foundry.core.errors import ConfigError
from foundry.core.tool import RunContext

_MAX_READ_BYTES = 1_000_000


class ReadFileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    """Absolute, or relative to the repository root."""


class FileContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str
    size_bytes: int
    modified_at: datetime


class WriteFileIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    content: str


class WriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    bytes_written: int
    is_new: bool
    is_overwrite: bool


def make_read_file(
    mctx: MetaToolContext,
) -> Callable[[ReadFileIn, RunContext], Awaitable[FileContent]]:
    async def handle(inputs: ReadFileIn, ctx: RunContext) -> FileContent:
        path = check_read_path(mctx, ctx.session, inputs.path, tool="read_file")
        if not path.is_file():
            raise ConfigError(
                f"read_file: no file at {path}",
                context={"path": str(path)},
            )
        size = path.stat().st_size
        if size > _MAX_READ_BYTES:
            raise ConfigError(
                f"read_file: {path} is {size} bytes (> {_MAX_READ_BYTES}); "
                "the meta-agent has no business reading files this large",
                context={"path": str(path), "size_bytes": size},
            )
        data = path.read_bytes()
        if b"\x00" in data:
            raise ConfigError(
                f"read_file: {path} looks binary (NUL bytes); text only",
                context={"path": str(path)},
            )
        return FileContent(
            path=str(path),
            content=data.decode("utf-8", errors="replace"),
            size_bytes=size,
            modified_at=datetime.fromtimestamp(path.stat().st_mtime, tz=UTC),
        )

    return handle


def make_write_file(
    mctx: MetaToolContext,
) -> Callable[[WriteFileIn, RunContext], Awaitable[WriteResult]]:
    async def handle(inputs: WriteFileIn, ctx: RunContext) -> WriteResult:
        path = check_write_path(
            mctx, ctx.session, inputs.path, tool="write_file"
        )
        if "\x00" in inputs.content:
            raise ConfigError(
                "write_file: refusing binary content (NUL bytes); the "
                "meta-agent writes text configs and code only",
                context={"path": str(path)},
            )
        existed = path.is_file()
        prior = path.read_text() if existed else None
        path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic: temp file + rename — no half-written file is ever visible.
        tmp = path.with_name(path.name + ".foundry-meta-tmp")
        tmp.write_text(inputs.content)
        os.replace(tmp, path)
        if path.suffix == ".py":
            # The artifact loader caches modules by file; a rewritten
            # handler/schema must be re-imported on the next eval.
            from foundry.catalog.loader import invalidate_artifact_module

            invalidate_artifact_module(path)
        mctx.records.files_written.append(str(path))
        return WriteResult(
            path=str(path),
            bytes_written=len(inputs.content.encode()),
            is_new=not existed,
            is_overwrite=existed and prior != inputs.content,
        )

    return handle


__all__ = [
    "FileContent",
    "ReadFileIn",
    "WriteFileIn",
    "WriteResult",
    "make_read_file",
    "make_write_file",
]
