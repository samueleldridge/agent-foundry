"""Path sandbox — the structural fs boundary (docs/83 § Meta-agent sandbox).

Consolidates the path-scoping rules the meta-agent's ``read_file`` /
``write_file`` tools enforce (docs/61, docs/86 invariants 3-4) into one
reusable, side-effect-free checker:

- **Canonicalisation first.** Every supplied path is resolved (symlinks
  followed, ``..`` collapsed) *before* any membership check, so traversal
  and symlink escapes are caught structurally.
- **Reads** are limited to an allowlist of roots (scoped project +
  framework root + catalog roots for the meta-agent).
- **Writes** are limited to a single root (the scoped project), with
  denied first-level subtrees (``evals/`` — the eval is the target;
  ``.foundry/`` — the audit trail doesn't move). Denied-subtree matching
  is case-insensitive (casefolded): on case-insensitive filesystems
  (darwin's APFS default) ``Evals/`` IS ``evals/``, so a case-sensitive
  check would let writes bypass the guard.

The checker raises :class:`foundry.core.errors.SandboxViolation` and does
nothing else: recording the violation, cancelling the forge session, and
mapping to a tool-facing error stay with the caller (the configurator),
so this module is usable by any future tool surface that needs fs scoping.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from foundry.core.errors import SandboxViolation


@dataclass(frozen=True)
class PathSandbox:
    """Allowlist + path-restriction checker.

    ``base_dir`` anchors relative paths (the repo root for meta-tools).
    ``read_roots`` is the read allowlist; ``write_root`` the single
    writable tree (``None`` forbids all writes). ``denied_write_subdirs``
    are first-level directories under ``write_root`` that stay read-only.
    """

    base_dir: Path
    read_roots: tuple[Path, ...] = ()
    write_root: Path | None = None
    denied_write_subdirs: tuple[str, ...] = ("evals", ".foundry")

    def resolve(self, raw: str | Path) -> Path:
        """Canonicalise: relative → under ``base_dir``; symlinks resolved
        BEFORE any membership check (docs/83 § sandboxes are structural).
        Paths the OS cannot canonicalise (e.g. an embedded null byte) are
        refused as violations — never surfaced as a bare ``ValueError``."""
        path = Path(raw)
        if not path.is_absolute():
            path = self.base_dir / path
        try:
            return path.resolve()
        except ValueError as exc:
            raise SandboxViolation(
                f"unresolvable path refused: {str(raw)!r} ({exc})",
                context={"path": str(raw), "kind": "resolve"},
            ) from exc

    def check_read(self, raw: str | Path) -> Path:
        path = self.resolve(raw)
        roots = tuple(root.resolve() for root in self.read_roots)
        if not any(_is_under(path, root) for root in roots):
            raise SandboxViolation(
                f"path outside sandbox: {path} (readable roots: "
                f"{', '.join(str(r) for r in roots)})",
                context={"path": str(path), "kind": "read"},
            )
        return path

    def check_write(self, raw: str | Path) -> Path:
        path = self.resolve(raw)
        if self.write_root is None:
            raise SandboxViolation(
                f"writes are not permitted by this sandbox: {path}",
                context={"path": str(path), "kind": "write"},
            )
        write_root = self.write_root.resolve()
        if not _is_under(path, write_root):
            raise SandboxViolation(
                f"write outside the scoped project: {path} (writes are "
                f"limited to {write_root}; catalog and framework trees are "
                "read-only — catalog promotion is human-gated)",
                context={"path": str(path), "kind": "write"},
            )
        relative = path.relative_to(write_root)
        denied = _denied_match(relative, self.denied_write_subdirs)
        if denied is not None:
            reason = {
                "evals": (
                    f"write into the eval set refused: {path} — the eval is "
                    "the target; the target doesn't move (docs/60)"
                ),
                ".foundry": (
                    f"write into the project's .foundry/ refused: {path} — "
                    "the audit log and runtime state are the framework's "
                    "record of what happened; the meta-agent cannot rewrite "
                    "its own audit trail (Phase 6 review finding 2)"
                ),
            }.get(denied, f"write into {denied}/ refused: {path}")
            raise SandboxViolation(
                reason,
                context={"path": str(path), "kind": "write", "denied": denied},
            )
        return path


def _is_under(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _denied_match(relative: Path, denied_subdirs: tuple[str, ...]) -> str | None:
    """Casefolded first-level match: ``Evals/`` and ``.Foundry/`` are the
    denied trees themselves on case-insensitive filesystems (darwin), so
    case must not matter (Phase 9 review follow-up 1)."""
    if not relative.parts:
        return None
    first = relative.parts[0].casefold()
    for name in denied_subdirs:
        if name.casefold() == first:
            return name
    return None


__all__ = ["PathSandbox"]
