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


__all__ = ["execute_catalog_promote"]
