"""ArtifactRef parsing and resolution (docs/12 § ArtifactRef parsing).

Phase 2a handles the ``tool`` and ``connection`` kinds; ``retriever`` and
``agent_template`` kinds are added in Phase 2b. Tools and connections share
one code path: the kind selects the subdirectory (``tools/`` vs
``connections/``), everything else — scope, roots walk, version discovery,
error reporting — is identical. That shared path is the exit-gate property
"catalog tool ref AND connection ref resolve through the same code path".
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field

from foundry.core.errors import RefResolutionError

_logger = logging.getLogger("foundry.config.refs")

ArtifactKind = Literal["tool", "connection"]

_KIND_SUBDIR: dict[str, str] = {"tool": "tools", "connection": "connections"}

_REF_RE = re.compile(
    r"^(?P<scope>catalog|local)/(?P<name>[a-z][a-z0-9_-]{0,63})"
    r"(?:@(?P<version>v\d+))?$"
)
_VERSION_DIR_RE = re.compile(r"^v(\d+)$")


class FoundryRoots(BaseModel):
    """Where catalogs and projects live on disk (docs/12 § FoundryRoots).

    Multi-root catalogs support the overlay pattern; resolution walks
    ``catalog_roots`` left-to-right, first hit wins, shadowing logs a warning.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_roots: list[Path] = Field(min_length=1)
    projects_root: Path
    project_name: str | None = None
    """If set, 'local/...' refs resolve against projects_root/<project_name>."""

    @classmethod
    def for_project(cls, project_dir: Path) -> Self:
        """Roots for a CLI invocation against one project directory.

        ``FOUNDRY_CATALOG_ROOTS`` (comma-separated) overrides catalog
        discovery; the default walks upward from the project directory to
        find a sibling ``catalog/`` tree (repo layout: catalog/ + projects/).
        """
        project_dir = project_dir.resolve()
        env_roots = os.environ.get("FOUNDRY_CATALOG_ROOTS", "")
        if env_roots:
            catalog_roots = [Path(p).resolve() for p in env_roots.split(",") if p]
        else:
            catalog_roots = []
            for ancestor in (project_dir, *project_dir.parents):
                candidate = ancestor / "catalog"
                if candidate.is_dir():
                    catalog_roots = [candidate]
                    break
            if not catalog_roots:
                # No catalog present — keep a deterministic (empty-on-disk)
                # root so local/ refs still resolve and catalog/ refs fail
                # with a path the user can see.
                catalog_roots = [project_dir.parent / "catalog"]
        return cls(
            catalog_roots=catalog_roots,
            projects_root=project_dir.parent,
            project_name=project_dir.name,
        )


class ArtifactRef(BaseModel):
    """A parsed 'catalog/<name>@v<N>' or 'local/<name>@v<N>' reference."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: Literal["catalog", "local"]
    kind: ArtifactKind
    name: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    version: str = Field(pattern=r"^v\d+$")

    @classmethod
    def parse(cls, s: str, kind: ArtifactKind, *, version: str | None = None) -> Self:
        """Parse 'catalog/query_db@v2' (or version-less 'catalog/query_db'
        plus an explicit ``version`` — the ToolBinding/ConnectionBinding
        shape, where the pin is a separate field)."""
        match = _REF_RE.match(s)
        if match is None:
            raise RefResolutionError(
                f"invalid artifact ref {s!r}; expected "
                "'catalog/<name>[@v<N>]' or 'local/<name>[@v<N>]'",
                context={"ref": s, "kind": kind},
            )
        inline_version = match.group("version")
        if inline_version is not None and version is not None and inline_version != version:
            raise RefResolutionError(
                f"artifact ref {s!r} pins {inline_version} but the binding "
                f"pins {version}; remove one of the two pins",
                context={"ref": s, "inline_version": inline_version,
                         "binding_version": version},
            )
        resolved_version = inline_version or version
        if resolved_version is None:
            raise RefResolutionError(
                f"artifact ref {s!r} has no version; pin one via '@v<N>' or "
                "the binding's `version:` field",
                context={"ref": s, "kind": kind},
            )
        return cls(
            scope=match.group("scope"),  # type: ignore[arg-type]
            kind=kind,
            name=match.group("name"),
            version=resolved_version,
        )

    def to_str(self) -> str:
        return f"{self.scope}/{self.name}@{self.version}"

    def artifact_dir(self, roots: FoundryRoots) -> Path:
        """The artifact's parent directory (holds v*/ + versions.json)."""
        subdir = _KIND_SUBDIR[self.kind]
        if self.scope == "local":
            if roots.project_name is None:
                raise RefResolutionError(
                    f"cannot resolve local ref {self.to_str()!r}: no project "
                    "is in scope (FoundryRoots.project_name is unset)",
                    context={"ref": self.to_str()},
                )
            base = roots.projects_root / roots.project_name / subdir / self.name
            if not base.is_dir():
                raise RefResolutionError(
                    f"{self.kind} {self.to_str()!r} not found at {base}",
                    context={"ref": self.to_str(), "checked": [str(base)]},
                )
            return base
        hits = [
            root / subdir / self.name
            for root in roots.catalog_roots
            if (root / subdir / self.name).is_dir()
        ]
        if not hits:
            checked = [str(root / subdir / self.name) for root in roots.catalog_roots]
            raise RefResolutionError(
                f"{self.kind} {self.to_str()!r} not found in any catalog root; "
                f"checked: {', '.join(checked)}",
                context={"ref": self.to_str(), "checked": checked},
            )
        if len(hits) > 1:
            _logger.warning(
                "catalog ref %s is shadowed: using %s, ignoring %s",
                self.to_str(), hits[0], [str(h) for h in hits[1:]],
            )
        return hits[0]

    def resolve_path(self, roots: FoundryRoots) -> Path:
        """The pinned version directory. Missing version → structured error
        listing the versions that DO exist (exit-gate behaviour)."""
        base = self.artifact_dir(roots)
        version_dir = base / self.version
        if not version_dir.is_dir():
            available = list_versions(base)
            raise RefResolutionError(
                f"{self.kind} {self.to_str()!r}: version {self.version!r} does "
                f"not exist at {base} (available: {', '.join(available) or 'none'})",
                context={
                    "ref": self.to_str(),
                    "resolved_dir": str(version_dir),
                    "available_versions": available,
                },
            )
        return version_dir


def list_versions(artifact_dir: Path) -> list[str]:
    """The v<N> subdirectories of an artifact dir, sorted numerically."""
    if not artifact_dir.is_dir():
        return []
    found: list[tuple[int, str]] = []
    for child in artifact_dir.iterdir():
        match = _VERSION_DIR_RE.match(child.name)
        if child.is_dir() and match:
            found.append((int(match.group(1)), child.name))
    return [name for _, name in sorted(found)]


def ref_matches_accept(ref: ArtifactRef, accept: str) -> bool:
    """Does a bound connection ref satisfy one ConnectionSlot.accepts entry?

    'catalog/postgres' accepts any version; 'catalog/postgres@v1' is exact
    (docs/12 § ConnectionSlot).
    """
    if "@" in accept:
        return ref.to_str() == accept
    return f"{ref.scope}/{ref.name}" == accept


__all__ = [
    "ArtifactKind",
    "ArtifactRef",
    "FoundryRoots",
    "list_versions",
    "ref_matches_accept",
]
