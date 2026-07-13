"""`foundry catalog promote` — human-gated promotion (docs/50, docs/03 § 5).

    foundry catalog promote <project>/<kind>/<name>
        [--floor 0.85] [--strict-semver] [--allow-breaking] [--yes]
        [--notes "..."]

Exit codes: 0 promoted, 1 refused (floor / overwrite / semver / declined),
2 configuration or unexpected failure.

`catalog list` / `catalog show` are Phase 9 dev-UX polish (docs/50 open
question 5); promotion is the only Phase 5 deliverable here.
"""

from __future__ import annotations

import sys
from pathlib import Path

from foundry.cli._helpers import print_foundry_error
from foundry.config.refs import FoundryRoots
from foundry.core.errors import (
    CatalogPromotionRefused,
    ConfigLoadError,
    FoundryError,
)
from foundry.observability.logging import configure_logging


def execute_catalog_promote(
    target: str,
    *,
    floor: float = 0.85,
    strict_semver: bool = False,
    allow_breaking: bool = False,
    assume_yes: bool = False,
    notes: str = "",
) -> int:
    configure_logging()
    try:
        from foundry.catalog.promote import promote_artifact
        from foundry.versioning.git_backend import GitBackend

        projects_root = Path("projects")
        catalog_root = Path("catalog")
        if not projects_root.is_dir() or not catalog_root.is_dir():
            raise ConfigLoadError(
                "run `foundry catalog promote` from the repo root (needs "
                "./projects/ and ./catalog/); "
                f"cwd is {Path.cwd()}",
                context={"cwd": str(Path.cwd())},
            )
        backend = GitBackend.discover(catalog_root.resolve())
        result = promote_artifact(
            target,
            projects_root=projects_root.resolve(),
            catalog_root=catalog_root.resolve(),
            backend=backend,
            floor=floor,
            strict_semver=strict_semver,
            allow_breaking=allow_breaking,
            confirm=(lambda _msg: True) if assume_yes else _confirm_breaking,
            notes=notes,
        )
        score = "n/a" if result.eval_score is None else f"{result.eval_score:.2f}"
        print(f"Promoted {result.source_ref} → {result.catalog_ref}")
        print(f"  eval score: {score} (floor {floor:.2f})")
        print(f"  schema change vs prior: {result.schema_change}")
        for change in result.breaking_changes:
            print(f"    - {change}")
        print(f"  commit: {result.commit_sha[:8]}")
        print("  versions.json + index.yaml updated; audit entry written.")
        return 0
    except CatalogPromotionRefused as exc:
        print_foundry_error(exc)
        return 1
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2


def _confirm_breaking(warning: str) -> bool:
    print(f"WARNING: {warning}")
    if not sys.stdin.isatty():
        print(
            "stdin is not a TTY; pass --yes (or --strict-semver "
            "--allow-breaking) to promote non-interactively.",
            file=sys.stderr,
        )
        return False
    reply = input("Promote anyway? [y/N] ")
    return reply.strip().lower() in ("y", "yes")


def _roots() -> FoundryRoots:
    import os

    env = os.environ.get("FOUNDRY_CATALOG_ROOTS", "")
    if env.strip():
        catalog_roots = [Path(p.strip()) for p in env.split(",") if p.strip()]
    else:
        catalog_roots = [Path("catalog")]
    catalog_roots = [root for root in catalog_roots if root.is_dir()]
    if not catalog_roots:
        raise ConfigLoadError(
            "no catalog roots found; run from the repo root or set "
            "FOUNDRY_CATALOG_ROOTS",
            context={"env": env or None},
        )
    return FoundryRoots(catalog_roots=catalog_roots, projects_root=Path("projects"))


def execute_catalog_list(*, kind: str | None = None, json_output: bool = False) -> int:
    """`foundry catalog list [--kind tools|connections|retrievers]` (docs/82)."""
    import json as json_module

    from foundry.catalog.loader import catalog_entries

    try:
        entries = catalog_entries(_roots())
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2
    singular = {"tools": "tool", "connections": "connection",
                "retrievers": "retriever"}.get(kind or "", kind)
    if singular:
        entries = [e for e in entries if e.kind == singular]
    rows = [
        {
            "ref": f"catalog/{e.name}",
            "kind": e.kind,
            "versions": e.versions,
            "latest": e.latest,
            "root": e.root,
        }
        for e in entries
    ]
    if json_output:
        print(json_module.dumps(rows, indent=2))
        return 0
    if not entries:
        print("(no catalog artifacts found)")
        return 0
    width = max(len(f"catalog/{e.name}") for e in entries)
    for e in entries:
        ref_str = f"catalog/{e.name}"
        versions = ", ".join(e.versions) or "-"
        latest = f" (latest {e.latest})" if e.latest else ""
        print(f"{ref_str:<{width}}  {e.kind:<10}  {versions}{latest}")
    return 0


def execute_catalog_show(ref: str, *, json_output: bool = False) -> int:
    """`foundry catalog show <name>` — versions.json + on-disk versions."""
    import json as json_module

    from foundry.catalog.loader import catalog_entries

    name = ref.removeprefix("catalog/")
    try:
        entries = [e for e in catalog_entries(_roots()) if e.name == name]
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2
    if not entries:
        print(f"catalog artifact {ref!r} not found", file=sys.stderr)
        return 2
    entry = entries[0]
    kind_dir = {"tool": "tools", "connection": "connections",
                "retriever": "retrievers"}[entry.kind]
    artifact_dir = Path(entry.root) / kind_dir / entry.name
    versions_file = artifact_dir / "versions.json"
    detail = {
        "ref": f"catalog/{entry.name}",
        "kind": entry.kind,
        "root": entry.root,
        "versions": entry.versions,
        "latest": entry.latest,
        "versions_metadata": (
            json_module.loads(versions_file.read_text())
            if versions_file.exists()
            else None
        ),
    }
    if json_output:
        print(json_module.dumps(detail, indent=2))
        return 0
    print(f"catalog/{entry.name}  ({entry.kind}, root {entry.root})")
    print(f"  versions: {', '.join(entry.versions) or '-'}")
    if entry.latest:
        print(f"  latest:   {entry.latest}")
    if detail["versions_metadata"] is not None:
        print("  versions.json:")
        print(
            "    "
            + json_module.dumps(detail["versions_metadata"], indent=2).replace(
                "\n", "\n    "
            )
        )
    return 0


__all__ = [
    "execute_catalog_list",
    "execute_catalog_promote",
    "execute_catalog_show",
]
