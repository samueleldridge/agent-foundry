"""ArtifactRef resolution against the on-disk structure (docs/50 § ArtifactRef).

Integrates with (does NOT duplicate) ``foundry.config.refs``: parsing of the
2-segment ``<scope>/<name>@<version>`` form, roots-walking, and version-dir
resolution all live there. This module adds the pieces docs/50 specifies on
top of that:

- the canonical 3-segment form ``<scope>/<kind>/<name>@<version>`` (kind
  optional, defaults to ``tool``; e.g. ``catalog/connections/pgvector@v1``);
- version-contiguity checking (``v1``, ``v3`` with no ``v2`` is a
  ``ConfigError`` — docs/50 invariant 2);
- latest-version discovery.
"""

from __future__ import annotations

from pathlib import Path

from foundry.config.refs import (
    ArtifactKind,
    ArtifactRef,
    FoundryRoots,
    list_versions,
)
from foundry.core.errors import ConfigError, RefResolutionError

_KIND_SEGMENTS: dict[str, ArtifactKind] = {
    "tool": "tool",
    "tools": "tool",
    "connection": "connection",
    "connections": "connection",
    "retriever": "retriever",
    "retrievers": "retriever",
    "agent_template": "agent_template",
    "agent_templates": "agent_template",
}


def parse_artifact_ref(
    ref: str, *, default_kind: ArtifactKind = "tool", version: str | None = None
) -> ArtifactRef:
    """Parse the canonical docs/50 form ``<scope>/<kind?>/<name>@<version>``.

    ``catalog/query_db@v2`` (kind omitted → tool) and
    ``local/connections/internal_api@v3`` both parse. ``version`` may be
    supplied separately for the binding shape (pin lives in a sibling field).
    """
    segments = ref.split("/")
    if len(segments) == 3:
        scope, kind_segment, rest = segments
        kind = _KIND_SEGMENTS.get(kind_segment)
        if kind is None:
            raise RefResolutionError(
                f"invalid artifact ref {ref!r}: unknown kind segment "
                f"{kind_segment!r} (expected one of: "
                f"{', '.join(sorted(set(_KIND_SEGMENTS)))})",
                context={"ref": ref, "kind_segment": kind_segment},
            )
        return ArtifactRef.parse(f"{scope}/{rest}", kind, version=version)
    return ArtifactRef.parse(ref, default_kind, version=version)


def resolve_version_dir(ref: ArtifactRef, roots: FoundryRoots) -> Path:
    """The pinned version directory (delegates to ``ArtifactRef.resolve_path``;
    missing versions raise ``RefResolutionError`` listing what DOES exist)."""
    return ref.resolve_path(roots)


def check_version_contiguity(artifact_dir: Path) -> list[str]:
    """Versions on disk, verified contiguous from v1 (docs/50 invariant 2).
    Returns the sorted version list; raises ``ConfigError`` on holes."""
    versions = list_versions(artifact_dir)
    expected = [f"v{i}" for i in range(1, len(versions) + 1)]
    if versions != expected:
        missing = sorted(set(expected) - set(versions))
        raise ConfigError(
            f"version numbering at {artifact_dir} is not contiguous: found "
            f"{', '.join(versions) or '(none)'}; missing "
            f"{', '.join(missing) or '(unexpected numbering)'} "
            "(docs/50: no gaps in v1, v2, ...)",
            context={
                "artifact_dir": str(artifact_dir),
                "found": versions,
                "missing": missing,
            },
        )
    return versions


def latest_version(artifact_dir: Path) -> str | None:
    """The highest contiguous version on disk, or None for a fresh artifact."""
    versions = check_version_contiguity(artifact_dir)
    return versions[-1] if versions else None


__all__ = [
    "check_version_contiguity",
    "latest_version",
    "parse_artifact_ref",
    "resolve_version_dir",
]
