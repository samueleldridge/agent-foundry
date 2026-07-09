"""Per-artifact version I/O (docs/03 § Phase 5 deliverable 2).

Directory-versioned artifacts (tools, connections — ``v<N>/``) and
file-versioned prompts (``v<N>.md``): create the NEXT version, list what
exists, and read/write the sibling ``versions.json`` metadata. Contiguity is
enforced on every write path — a new version is always exactly latest+1
(docs/50 invariants 1-2).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from foundry.catalog.schemas import VersionMetadata, VersionsMetadata
from foundry.core.errors import ConfigError, VersioningError
from foundry.versioning.refs import check_version_contiguity

_PROMPT_FILE_RE = re.compile(r"^v(\d+)\.md$")

KIND_SUBDIRS: dict[str, str] = {
    "tool": "tools",
    "connection": "connections",
    "retriever": "retrievers",
}


# --- directory-versioned artifacts (tools / connections / retrievers) ------------


def artifact_dir(project_dir: Path, kind: str, name: str) -> Path:
    """``projects/<p>/<kinds>/<name>`` for a project-local artifact."""
    subdir = KIND_SUBDIRS.get(kind)
    if subdir is None:
        raise VersioningError(
            f"unknown artifact kind {kind!r} (expected one of: "
            f"{', '.join(sorted(KIND_SUBDIRS))})",
            context={"kind": kind, "name": name},
        )
    return project_dir / subdir / name


def list_artifact_versions(directory: Path) -> list[str]:
    """The contiguous ``v<N>/`` versions under an artifact directory."""
    return check_version_contiguity(directory)


def next_version_name(directory: Path) -> str:
    """The next version to create: latest+1, ``v1`` for a fresh artifact."""
    versions = check_version_contiguity(directory)
    return f"v{len(versions) + 1}"


def create_next_version_dir(directory: Path) -> Path:
    """Create (and return) the next ``v<N>/`` under an artifact directory.
    The new directory is empty; the caller writes the version's files."""
    version_dir = directory / next_version_name(directory)
    version_dir.mkdir(parents=True, exist_ok=False)
    return version_dir


# --- file-versioned prompts (docs/50 axis 2) ----------------------------------------


def prompts_dir(project_dir: Path, agent: str) -> Path:
    return project_dir / "agents" / agent / "prompts"


def list_prompt_versions(directory: Path) -> list[str]:
    """The ``v<N>`` prompt versions in a prompts/ directory, verified
    contiguous from v1 (same discipline as directory versions)."""
    if not directory.is_dir():
        return []
    found: list[tuple[int, str]] = []
    for child in directory.iterdir():
        match = _PROMPT_FILE_RE.match(child.name)
        if child.is_file() and match:
            found.append((int(match.group(1)), f"v{match.group(1)}"))
    versions = [name for _, name in sorted(found)]
    expected = [f"v{i}" for i in range(1, len(versions) + 1)]
    if versions != expected:
        missing = sorted(set(expected) - set(versions))
        raise ConfigError(
            f"prompt numbering at {directory} is not contiguous: found "
            f"{', '.join(versions) or '(none)'}; missing "
            f"{', '.join(missing) or '(unexpected numbering)'} (docs/50)",
            context={
                "prompts_dir": str(directory),
                "found": versions,
                "missing": missing,
            },
        )
    return versions


def next_prompt_path(directory: Path) -> Path:
    """The path of the NEXT prompt version file (not created — prompts are
    single files; the caller writes the content)."""
    versions = list_prompt_versions(directory)
    return directory / f"v{len(versions) + 1}.md"


# --- versions.json metadata ---------------------------------------------------------


def versions_metadata_path(directory: Path) -> Path:
    return directory / "versions.json"


def read_versions_metadata(directory: Path) -> VersionsMetadata | None:
    """The artifact's ``versions.json``, or None when absent (legal for
    project-local artifacts that never recorded metadata)."""
    path = versions_metadata_path(directory)
    if not path.exists():
        return None
    # Reuse the loader's error ergonomics (structured ConfigLoadError /
    # ConfigValidationError with file + pointer).
    from foundry.catalog.loader import load_versions_metadata

    return load_versions_metadata(path)


def write_versions_metadata(directory: Path, metadata: VersionsMetadata) -> Path:
    """Write ``versions.json`` (2-space indent, trailing newline)."""
    path = versions_metadata_path(directory)
    payload = json.dumps(
        metadata.model_dump(mode="json", exclude_none=True), indent=2
    )
    path.write_text(payload + "\n")
    return path


def append_version_metadata(
    directory: Path, entry: VersionMetadata
) -> VersionsMetadata:
    """Append one version's metadata to the artifact's ``versions.json``
    (created if absent). Re-recording an existing version is refused —
    version directories are immutable (docs/50 invariant 1)."""
    metadata = read_versions_metadata(directory) or VersionsMetadata(versions=[])
    if metadata.get(entry.version) is not None:
        raise VersioningError(
            f"versions.json at {directory} already records {entry.version}; "
            "version metadata is immutable once written (docs/50 invariant 1)",
            context={"artifact_dir": str(directory), "version": entry.version},
        )
    metadata.versions.append(entry)
    write_versions_metadata(directory, metadata)
    return metadata


__all__ = [
    "KIND_SUBDIRS",
    "append_version_metadata",
    "artifact_dir",
    "create_next_version_dir",
    "list_artifact_versions",
    "list_prompt_versions",
    "next_prompt_path",
    "next_version_name",
    "prompts_dir",
    "read_versions_metadata",
    "versions_metadata_path",
    "write_versions_metadata",
]
