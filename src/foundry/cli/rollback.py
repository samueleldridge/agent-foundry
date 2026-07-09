"""`foundry rollback` — per-tool, per-prompt, per-project rollback
(docs/52 § CLI surface).

    foundry rollback <project> --tool <name> --to <vN>      # pin edit only
    foundry rollback <project> --prompt <agent> --to <vN>   # pin edit only
    foundry rollback <project> --to <commit>                # whole subtree
    ... [--dry-run] [--force] [--yes]

Exit codes: 0 applied (or --dry-run), 1 refused (pre-flight failure /
operator abort), 2 configuration or unexpected failure.
"""

from __future__ import annotations

import sys

from foundry.cli._helpers import print_foundry_error, resolve_project_dir
from foundry.core.errors import ConfigValidationError, FoundryError, RollbackError
from foundry.observability.logging import configure_logging
from foundry.versioning.git_backend import GitBackend
from foundry.versioning.rollback import (
    RollbackPlan,
    execute_rollback,
    plan_project_rollback,
    plan_prompt_rollback,
    plan_tool_rollback,
)


def execute_rollback_command(
    project: str,
    *,
    tool: str | None = None,
    prompt: str | None = None,
    to: str | None = None,
    force: bool = False,
    dry_run: bool = False,
    assume_yes: bool = False,
) -> int:
    """The `foundry rollback` implementation. Returns the exit code."""
    configure_logging()
    try:
        if to is None:
            raise ConfigValidationError(
                "usage: foundry rollback <project> [--tool <name> | "
                "--prompt <agent>] --to <version|commit>",
                context={"project": project},
            )
        if tool is not None and prompt is not None:
            raise ConfigValidationError(
                "--tool and --prompt are mutually exclusive (one rollback, "
                "one artifact — docs/52)",
                context={"tool": tool, "prompt": prompt},
            )
        project_dir = resolve_project_dir(project)
        backend = GitBackend.discover(project_dir)
        plan: RollbackPlan
        if tool is not None:
            plan = plan_tool_rollback(project_dir, tool, to, backend=backend)
        elif prompt is not None:
            plan = plan_prompt_rollback(project_dir, prompt, to, backend=backend)
        else:
            plan = plan_project_rollback(project_dir, to, backend=backend)

        print(plan.render())
        if dry_run:
            print("\n--dry-run: no changes applied, no commit, no audit entry.")
            return 0

        confirmed = assume_yes or force
        if not confirmed:
            if not _confirm(f"\nApply this {plan.granularity} rollback? [y/N] "):
                print("Aborted; nothing changed.")
                return 1
            # An interactive `y` IS the confirmation — it satisfies
            # confirm-class pre-flight checks (e.g. schema_compatible)
            # without requiring a --yes rerun. Phase 5 review finding 3.
            confirmed = True

        result = execute_rollback(
            plan, backend=backend, force=force, assume_yes=confirmed
        )
        print(f"\nApplied. Commit: {result.commit_sha[:8]}")
        print(f"  {plan.commit_message.splitlines()[0]}")
        print(f"Audit entry written ({result.audit_entry.id}).")
        if result.overrides_used:
            print(
                "  overrides_used: "
                + ", ".join(result.overrides_used)
                + "  (logged loudly to audit — docs/52)"
            )
        for note in result.notes:
            print(f"  note: {note}")
        if plan.granularity in ("tool", "prompt"):
            print(
                f"  {plan.current} stays on disk; roll forward with "
                f"--to {plan.current}."
            )
        return 0
    except RollbackError as exc:
        print_foundry_error(exc)
        return 1
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2


def _confirm(message: str) -> bool:
    if not sys.stdin.isatty():
        print(
            "stdin is not a TTY; pass --yes (or --force) to apply "
            "non-interactively.",
            file=sys.stderr,
        )
        return False
    reply = input(message)
    return reply.strip().lower() in ("y", "yes")


__all__ = ["execute_rollback_command"]
