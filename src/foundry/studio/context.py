"""Shared control-plane state threaded through every studio route module.

One :class:`StudioContext` per ``create_studio_app`` call. It carries the
repo layout (repo root / projects root / catalog roots), the optional
provider-transport substitution (tests), the per-project compiled-project
cache, and the long-lived registries (chat pool, forge supervisor, task
registry) that the app lifespan binds to its task group.

This module holds NO business logic — it is plumbing for the route
modules, which delegate to the framework proper (docs/72 § Module layout).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from foundry.core.errors import ConfigLoadError, ProjectUnavailableError
from foundry.runtime.compiled import CompiledProject
from foundry.security.sandbox import PathSandbox
from foundry.versioning.git_backend import GitBackend

if TYPE_CHECKING:  # circular-import guards (registries live in sibling modules)
    from foundry.studio.chat import ChatRegistry
    from foundry.studio.forge import ForgeSupervisor
    from foundry.studio.tasks import TaskRegistry

STUDIO_VERSION = "0.1.0"
"""Mirrors pyproject [project].version; stamped on every response."""


@dataclass(frozen=True)
class StudioSettings:
    """`foundry studio` invocation knobs (docs/72 § CLI)."""

    host: str = "127.0.0.1"
    port: int = 8400
    dev: bool = False
    open_browser: bool = True
    auth_token: str | None = None


@dataclass
class StudioContext:
    """Everything a studio route module needs, resolved once at app build."""

    repo_root: Path
    auth_token: str | None = None
    checkpoint: str = "sqlite"
    transport: httpx.AsyncBaseTransport | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    version: str = STUDIO_VERSION

    # Long-lived registries; populated by create_studio_app.
    chat: ChatRegistry | None = None
    forge: ForgeSupervisor | None = None
    tasks: TaskRegistry | None = None

    _tg: Any = None
    _compiled_cache: dict[str, CompiledProject] = field(default_factory=dict)

    # --- repo layout -------------------------------------------------------------

    @property
    def projects_root(self) -> Path:
        return self.repo_root / "projects"

    def catalog_roots(self) -> list[Path]:
        env = os.environ.get("FOUNDRY_CATALOG_ROOTS", "")
        if env.strip():
            roots = [Path(p.strip()) for p in env.split(",") if p.strip()]
        else:
            roots = [self.repo_root / "catalog"]
        return [root for root in roots if root.is_dir()]

    def project_names(self) -> list[str]:
        root = self.projects_root
        if not root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in root.iterdir()
            if (entry / "system.yaml").is_file()
        )

    def bootstrap_project_names(self) -> list[str]:
        """``foundry project new`` skeletons: a project directory WITHOUT
        a system.yaml yet (forge-able, not runnable)."""
        root = self.projects_root
        if not root.is_dir():
            return []
        return sorted(
            entry.name
            for entry in root.iterdir()
            if entry.is_dir()
            and not entry.name.startswith(".")
            and not (entry / "system.yaml").is_file()
        )

    def project_dir(self, name: str, *, allow_bootstrap: bool = False) -> Path:
        """Resolve a project NAME to its directory — 404-shaped error when
        missing (same contract as the CLI's resolve_project_dir).

        ``allow_bootstrap=True`` accepts a ``foundry project new`` skeleton
        (a project directory with no system.yaml yet) — the config-editor
        and forge surfaces work on those; run-shaped surfaces do not."""
        candidate = (self.projects_root / name).resolve()
        if not candidate.is_relative_to(self.projects_root.resolve()):
            raise ConfigLoadError(
                f"invalid project name {name!r}",
                context={"project": name},
            )
        if not (candidate / "system.yaml").is_file():
            if allow_bootstrap and candidate.is_dir():
                return candidate
            raise ConfigLoadError(
                f"project {name!r} not found under {self.projects_root} "
                "(need a directory containing system.yaml)",
                context={"project": name, "not_found": True},
            )
        return candidate

    def backend(self) -> GitBackend:
        return GitBackend.discover(self.repo_root)

    def sandbox_for(
        self, project: str, *, allow_bootstrap: bool = False
    ) -> PathSandbox:
        """The meta-agent-shaped write sandbox, scoped to one project:
        writes only under ``projects/<name>``; ``evals/`` + ``.foundry/``
        stay read-only (docs/72 § Security posture)."""
        project_dir = self.project_dir(project, allow_bootstrap=allow_bootstrap)
        return PathSandbox(
            base_dir=self.repo_root,
            read_roots=(self.repo_root,),
            write_root=project_dir,
        )

    # --- compiled-project cache ---------------------------------------------------

    def compiled(self, project: str) -> CompiledProject:
        """Compile (or reuse) a project. Invalidated on config writes and
        rollbacks so chat/graph always reflect the committed tree.

        A compile that fails ONLY because a credentials env var is unset
        re-raises as :class:`ProjectUnavailableError` (HTTP 424) — the
        project's stored state stays browsable; compile semantics are
        untouched (studio-surface handling only)."""
        cached = self._compiled_cache.get(project)
        if cached is not None:
            return cached
        from foundry.orchestration.compiler import compile_project

        project_dir = self.project_dir(project)  # 404 first, before compile
        try:
            compiled = compile_project(project_dir, transport=self.transport)
        except ConfigLoadError as exc:
            env_var = exc.context.get("env_var")
            if not env_var:
                raise
            raise ProjectUnavailableError(
                f"project {project!r} is unavailable: environment variable "
                f"{env_var!r} is not set",
                project=project,
                env_vars=[str(env_var)],
                remedy=(
                    f"set {env_var} in the environment (e.g. the backend "
                    "repo's .env) and restart foundry studio, or edit the "
                    "connection to a different credentials_ref"
                ),
                cause=exc,
            ) from exc
        self._compiled_cache[project] = compiled
        return compiled

    def invalidate(self, project: str) -> None:
        self._compiled_cache.pop(project, None)
        if self.chat is not None:
            self.chat.invalidate(project)

    # --- lifespan task group ---------------------------------------------------------

    def bind(self, task_group: Any) -> None:
        self._tg = task_group

    @property
    def task_group(self) -> Any:
        if self._tg is None:
            raise ConfigLoadError(
                "studio app lifespan has not started — run the ASGI app's "
                "lifespan (tests: TestClient(app) as a context manager)",
                context={"repo_root": str(self.repo_root)},
            )
        return self._tg

    def spawn(self, fn: Any, *args: Any) -> None:
        if self._tg is None:
            raise ConfigLoadError(
                "studio app lifespan has not started — run the ASGI app's "
                "lifespan (tests: TestClient(app) as a context manager)",
                context={"repo_root": str(self.repo_root)},
            )
        self._tg.start_soon(fn, *args)

    def uptime_s(self) -> float:
        return (datetime.now(UTC) - self.started_at).total_seconds()


__all__ = ["STUDIO_VERSION", "StudioContext", "StudioSettings"]
