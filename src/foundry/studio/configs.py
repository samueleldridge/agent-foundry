"""Config file tree / read / validate / write-with-commit (docs/72 §
Configs + validation).

Validation delegates ENTIRELY to the ``foundry.config`` loaders — the
studio layer never re-parses YAML. Candidate content is validated against
a shadow copy of the project (so ``extends:`` bases resolve exactly as
they would on disk) and the loader's structured ``ConfigValidationError``
context maps 1:1 onto :class:`ValidationIssue` — identical message text to
the CLI, positioned for the editor gutter.

The write path is validate → sandbox → write → commit
``studio(<project>): edit <path>`` via the versioning helpers, with an
audit entry carrying ``operator.kind = "studio"``.
"""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from foundry.config.loader import (
    load_agent_spec,
    load_connection_spec,
    load_eval_spec,
    load_function_node_spec,
    load_retriever_spec,
    load_state_spec,
    load_system_spec,
    load_tool_spec,
)
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
from foundry.core.errors import (
    ConfigError,
    ConfigLoadError,
    FoundryError,
    SandboxViolation,
)
from foundry.studio.context import StudioContext
from foundry.studio.events import emit_studio_event
from foundry.studio.schemas import (
    ConfigKind,
    FileContent,
    FileEntry,
    FileTree,
    ValidateRequest,
    ValidationIssue,
    ValidationResult,
    WriteRequest,
    WriteResult,
)
from foundry.studio.security import studio_operator
from foundry.versioning.audit import append_audit_entry, new_audit_entry

KIND_MODELS: dict[str, type[BaseModel]] = {
    "system": SystemSpec,
    "state": StateSpec,
    "agent": AgentSpec,
    "tool": ToolSpec,
    "connection": ConnectionSpec,
    "retriever": RetrieverSpec,
    "eval": EvalSpec,
    "function": FunctionNodeSpec,
}

_KIND_LOADERS: dict[str, Any] = {
    "system": load_system_spec,
    "state": load_state_spec,
    "agent": load_agent_spec,
    "tool": load_tool_spec,
    "connection": load_connection_spec,
    "retriever": load_retriever_spec,
    "eval": load_eval_spec,
    "function": load_function_node_spec,
}

_SKIP_DIRS = {".foundry", "__pycache__", ".pytest_cache", "tests"}


def kind_for(project_dir: Path, rel_path: str) -> ConfigKind:
    """Infer the config kind from the project-relative path — the same
    directory/filename conventions ``load_project`` walks (docs/12)."""
    parts = Path(rel_path).parts
    name = Path(rel_path).name
    suffix = Path(rel_path).suffix
    if rel_path == "system.yaml":
        return "system"
    if suffix in (".yaml", ".yml"):
        if len(parts) == 1:
            # The state spec is whatever system.yaml points at (default
            # state.yaml); any other top-level yaml is unknown.
            state_name = _state_file_name(project_dir)
            return "state" if name == state_name else "other"
        if parts[0] == "agents" and name == "agent.yaml":
            return "agent"
        if parts[0] == "tools" and name == "tool.yaml":
            return "tool"
        if parts[0] == "connections" and name == "connection.yaml":
            return "connection"
        if parts[0] == "retrievers" and name == "retriever.yaml":
            return "retriever"
        if parts[0] == "functions" and name == "function.yaml":
            return "function"
        if parts[0] == "evals" or name in ("eval.yaml", "health.yaml"):
            return "eval"
        if parts[0] == "agents" and len(parts) >= 3 and parts[2] == "eval":
            return "eval"
        return "other"
    if suffix == ".md":
        if parts[0] == "agents" and "prompts" in parts:
            return "prompt"
        return "markdown"
    if suffix == ".py":
        return "python"
    return "other"


def _state_file_name(project_dir: Path) -> str:
    import yaml

    try:
        data = yaml.safe_load((project_dir / "system.yaml").read_text())
    except Exception:
        return "state.yaml"
    if isinstance(data, dict) and isinstance(data.get("state"), str):
        return str(data["state"])
    return "state.yaml"


def _editable(rel_path: str) -> bool:
    """The sandbox denies ``evals/`` + ``.foundry/`` writes; everything
    else in the project tree is studio-editable."""
    first = Path(rel_path).parts[0].casefold() if Path(rel_path).parts else ""
    return first not in ("evals", ".foundry")


def list_files(project_dir: Path) -> list[FileEntry]:
    entries: list[FileEntry] = []
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(project_dir).parts
        if any(part in _SKIP_DIRS for part in rel_parts[:-1]):
            continue
        if path.name.startswith("."):
            continue
        rel = path.relative_to(project_dir).as_posix()
        if path.suffix not in (".yaml", ".yml", ".md", ".py"):
            continue
        entries.append(
            FileEntry(
                path=rel,
                kind=kind_for(project_dir, rel),
                editable=_editable(rel),
            )
        )
    return entries


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# --- validation ---------------------------------------------------------------------


def _issue_from_error(
    exc: FoundryError, *, shadow_dir: Path, project_dir: Path
) -> ValidationIssue:
    """Loader error → ValidationIssue. The shadow-copy path is rewritten
    back to the real project path so the message text is character-
    identical to what the CLI prints against the working tree."""
    message = str(exc)
    # Resolved prefix FIRST: on darwin the unresolved tmp prefix
    # (/var/...) is a substring of the resolved one (/private/var/...),
    # so replacement order matters.
    prefixes = [str(shadow_dir.resolve()), str(shadow_dir)]
    for prefix in dict.fromkeys(prefixes):
        message = message.replace(prefix, str(project_dir))
    context = exc.context
    line = context.get("line")
    column = context.get("column")
    return ValidationIssue(
        severity="error",
        message=message,
        pointer=(
            str(context["pointer"]) if context.get("pointer") else None
        ),
        line=int(line) if isinstance(line, int) else None,
        column=int(column) if isinstance(column, int) else None,
        hint=str(context["hint"]) if context.get("hint") else None,
    )


def validate_content(
    project_dir: Path, rel_path: str, content: str
) -> ValidationResult:
    """Validate candidate content WITHOUT writing to the project.

    A shadow copy of the project receives the candidate file, then the
    kind's loader runs against it — so ``extends:`` resolution, env
    interpolation, and the secret-literal scan behave exactly as a real
    load would (docs/72: the loaders are the single validator)."""
    kind = kind_for(project_dir, rel_path)
    if kind in ("prompt", "markdown"):
        issues = []
        if not content.strip():
            issues.append(
                ValidationIssue(
                    severity="warning",
                    message=f"{rel_path} is empty",
                )
            )
        return ValidationResult(ok=True, issues=issues, kind=kind)
    if kind == "python":
        try:
            compile(content, rel_path, "exec")
        except SyntaxError as exc:
            return ValidationResult(
                ok=False,
                kind=kind,
                issues=[
                    ValidationIssue(
                        severity="error",
                        message=f"python syntax error: {exc.msg}",
                        line=exc.lineno,
                        column=exc.offset,
                    )
                ],
            )
        return ValidationResult(ok=True, kind=kind)
    if kind == "other":
        return ValidationResult(
            ok=False,
            kind=kind,
            issues=[
                ValidationIssue(
                    severity="error",
                    message=(
                        f"{rel_path} does not match any config-file "
                        "convention this project recognises"
                    ),
                )
            ],
        )

    loader = _KIND_LOADERS[kind]
    with tempfile.TemporaryDirectory(prefix="foundry-studio-validate-") as tmp:
        shadow = Path(tmp) / project_dir.name
        shutil.copytree(
            project_dir,
            shadow,
            ignore=shutil.ignore_patterns(
                "__pycache__", ".foundry", ".pytest_cache"
            ),
        )
        target = shadow / rel_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        try:
            loader(target)
        except ConfigError as exc:
            return ValidationResult(
                ok=False,
                kind=kind,
                issues=[
                    _issue_from_error(
                        exc, shadow_dir=shadow, project_dir=project_dir
                    )
                ],
            )
    return ValidationResult(ok=True, kind=kind)


# --- routes -------------------------------------------------------------------------


def build_router(ctx: StudioContext) -> APIRouter:
    router = APIRouter()

    @router.get("/projects/{name}/files", response_model=FileTree)
    def files(name: str) -> FileTree:
        # allow_bootstrap: `foundry project new` skeletons are editable
        # surfaces (the starter eval deep-link) before system.yaml exists.
        project_dir = ctx.project_dir(name, allow_bootstrap=True)
        return FileTree(project=name, files=list_files(project_dir))

    @router.get(
        "/projects/{name}/files/{path:path}", response_model=FileContent
    )
    def read_file(name: str, path: str) -> FileContent:
        project_dir = ctx.project_dir(name, allow_bootstrap=True)
        sandbox = ctx.sandbox_for(name, allow_bootstrap=True)
        resolved = sandbox.check_read(project_dir / path)
        if not resolved.is_relative_to(project_dir) or not resolved.is_file():
            raise ConfigLoadError(
                f"file {path!r} not found in project {name!r}",
                context={"project": name, "path": path, "not_found": True},
            )
        kind = kind_for(project_dir, path)
        text = resolved.read_text()
        return FileContent(
            path=path,
            kind=kind,
            content=text,
            content_hash=content_hash(text),
            schema_url=(
                f"/api/schemas/{kind}" if kind in KIND_MODELS else None
            ),
            editable=_editable(path),
        )

    @router.post(
        "/projects/{name}/validate", response_model=ValidationResult
    )
    def validate(name: str, body: ValidateRequest) -> ValidationResult:
        project_dir = ctx.project_dir(name, allow_bootstrap=True)
        return validate_content(project_dir, body.path, body.content)

    @router.put("/projects/{name}/files/{path:path}")
    def write_file(
        name: str, path: str, body: WriteRequest, request: Request
    ) -> Any:
        project_dir = ctx.project_dir(name, allow_bootstrap=True)
        request_id = getattr(request.state, "studio_request_id", "")

        # 1. Sandbox FIRST: an out-of-tree path must never reach the
        #    validator or the filesystem (403 + studio.sandbox_refused).
        sandbox = ctx.sandbox_for(name, allow_bootstrap=True)
        try:
            resolved = sandbox.check_write(project_dir / path)
        except SandboxViolation:
            emit_studio_event(
                "studio.sandbox_refused",
                project=name,
                studio_request_id=request_id,
                path=path,
            )
            raise

        # 2. Validate (nothing written on failure — 422 + issues).
        result = validate_content(project_dir, path, body.content)
        if not result.ok:
            return JSONResponse(
                status_code=422,
                content=result.model_dump(mode="json"),
            )

        # 3. Concurrent-edit safety: stale editor content → 409 + diff.
        if resolved.is_file() and body.base_hash is not None:
            current = resolved.read_text()
            if content_hash(current) != body.base_hash:
                return JSONResponse(
                    status_code=409,
                    content={
                        "error_class": "StaleContent",
                        "message": (
                            f"{path} changed since the editor loaded it "
                            "(hash mismatch); reload and merge"
                        ),
                        "context": {
                            "path": path,
                            "current_hash": content_hash(current),
                            "server_content": current,
                        },
                    },
                )

        # 4. Write + commit via the versioning helpers.
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(body.content)
        backend = ctx.backend()
        message = f"studio({name}): edit {path}"
        commit_sha = backend.commit([resolved], message)
        entry = new_audit_entry(
            type="human",
            scope=f"{name}/{path}",
            summary=message,
            operator=studio_operator(),
            commit_sha=commit_sha,
            files_affected=[f"projects/{name}/{path}"],
        )
        append_audit_entry(project_dir, entry)
        emit_studio_event(
            "studio.config_saved",
            project=name,
            studio_request_id=request_id,
            path=path,
            commit_sha=commit_sha,
        )
        ctx.invalidate(name)
        return WriteResult(
            path=path, commit_sha=commit_sha, commit_message=message
        )

    @router.get("/schemas/{kind}")
    def config_schema(kind: str) -> dict[str, Any]:
        model = KIND_MODELS.get(kind)
        if model is None:
            raise ConfigLoadError(
                f"unknown config kind {kind!r}; known: "
                f"{', '.join(sorted(KIND_MODELS))}",
                context={"kind": kind, "not_found": True},
            )
        return model.model_json_schema()

    return router


__all__ = [
    "KIND_MODELS",
    "build_router",
    "content_hash",
    "kind_for",
    "list_files",
    "validate_content",
]
