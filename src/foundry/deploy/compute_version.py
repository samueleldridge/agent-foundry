"""`foundry compute-version` — the project's content-hashed system_version
(docs/84 § `foundry compute-version`, docs/50 § system_version).

The hash covers the project's git-trackable config tree ONLY: runtime state
(``.foundry/``), caches (``__pycache__``, ``.pytest_cache``) and hidden files
never contribute. Same project state ⇒ same hash, across runs and processes —
the hash is the image tag (``<project>:<system_version>``), so determinism is
the whole point.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from foundry.core.errors import ConfigLoadError, GitBackendError
from foundry.versioning.git_backend import GitBackend

_EXCLUDED_DIRS = frozenset({".foundry", "__pycache__", ".pytest_cache"})
_HASH_PREFIX_LEN = 16
_SHORT_SHA_LEN = 7


def _iter_config_files(project_dir: Path) -> list[Path]:
    """Every hashable file under ``project_dir``, sorted by relative POSIX
    path. Hidden files/directories and runtime-state directories are pruned."""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(project_dir):
        dirnames[:] = sorted(
            d
            for d in dirnames
            if d not in _EXCLUDED_DIRS and not d.startswith(".")
        )
        for filename in filenames:
            if filename.startswith("."):
                continue
            found.append(Path(dirpath) / filename)
    return sorted(found, key=lambda p: p.relative_to(project_dir).as_posix())


def compute_system_version(
    project_dir: Path, *, include_git_sha: bool = False
) -> str:
    """Deterministic sha256 content hash (first 16 hex chars) of the
    project's config tree.

    ``include_git_sha=True`` appends ``@<short sha>`` of the enclosing
    repository's HEAD (``cb861da9abcd1234@f1d1542``); when the project is not
    inside a git repository the plain content hash is returned — the hash is
    the load-bearing part, the sha is provenance garnish.
    """
    project_dir = Path(project_dir)
    if not project_dir.is_dir():
        raise ConfigLoadError(
            f"project directory {project_dir} does not exist",
            context={"project_dir": str(project_dir)},
        )
    digest = hashlib.sha256()
    for path in _iter_config_files(project_dir):
        rel = path.relative_to(project_dir).as_posix()
        digest.update(rel.encode("utf-8"))
        digest.update(b"\0")
        digest.update(path.read_bytes())
    version = digest.hexdigest()[:_HASH_PREFIX_LEN]
    if not include_git_sha:
        return version
    try:
        backend = GitBackend.discover(project_dir)
        short_sha = backend.rev_parse("HEAD")[:_SHORT_SHA_LEN]
    except GitBackendError:
        return version
    return f"{version}@{short_sha}"


__all__ = ["compute_system_version"]
