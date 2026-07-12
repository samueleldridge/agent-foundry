"""`python -m foundry` entry point.

Phase 0 deliberately ships only `--help`. Every subcommand below is registered
as a placeholder that exits with a "not yet implemented" message and points at
the phase that will land it. See docs/03-development-phases.md.

CLI framework: Typer (chosen over argparse for typed signatures and over click
for the lighter decorator surface; documented in pyproject.toml).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import NoReturn

import typer

app = typer.Typer(
    name="foundry",
    help=(
        "agent-foundry — build, evaluate, version, and orchestrate "
        "multi-agent LLM systems from declarative configs.\n\n"
        "`foundry run` (Phase 1), `foundry connections health` (Phase 2a), "
        "`foundry eval` (Phase 4), and `foundry rollback` / `versions` / "
        "`diff` / `catalog promote` (Phase 5) are live. Remaining "
        "subcommands land in later phases; see docs/03-development-phases.md."
    ),
    no_args_is_help=True,
    add_completion=False,
)


def _not_yet_implemented(command: str, phase: str) -> NoReturn:
    typer.echo(
        f"`foundry {command}` is not implemented yet (planned for {phase}). "
        f"See docs/03-development-phases.md.",
        err=True,
    )
    raise typer.Exit(code=2)


_PROJECT_PATH_ARG = typer.Argument(
    ..., help="Path to the project directory (containing system.yaml)."
)
_INPUT_OPTION = typer.Option(
    "{}",
    "--input",
    help='JSON object of run inputs, e.g. \'{"name": "world"}\'.',
)
_STREAM_OPTION = typer.Option(
    False,
    "--stream",
    help="Stream RunEvents incrementally to stdout as JSONL while the run "
    "executes (the final output prints last).",
)
_CHECKPOINT_OPTION = typer.Option(
    "memory",
    "--checkpoint",
    help="Checkpointer: 'memory' (per-process, default), 'sqlite' "
    "(survives the process; enables kill+resume), or 'none'.",
)
_RUN_ID_OPTION = typer.Option(
    None,
    "--run-id",
    help="Reuse an existing run id. With --checkpoint sqlite, an "
    "interrupted run with this id RESUMES from its last checkpoint.",
)


@app.command(help="Run a configured system end-to-end.")
def run(
    project_path: Path = _PROJECT_PATH_ARG,
    input_json: str = _INPUT_OPTION,
    stream: bool = _STREAM_OPTION,
    checkpoint: str = _CHECKPOINT_OPTION,
    run_id: str | None = _RUN_ID_OPTION,
) -> None:
    from foundry.cli.run import execute_run

    raise typer.Exit(
        code=execute_run(
            project_path,
            input_json,
            stream=stream,
            checkpoint=checkpoint,
            run_id=run_id,
        )
    )


_RESUME_RUN_ID_ARG = typer.Argument(
    ..., help="The paused run's id (printed when the run paused)."
)
_APPROVE_OPTION = typer.Option(
    False, "--approve", help="Approve the pending approval and continue."
)
_REJECT_OPTION = typer.Option(
    False,
    "--reject",
    help="Reject the pending approval (requires --reason); the agent sees "
    "the rejection and continues.",
)
_REASON_OPTION = typer.Option(
    None, "--reason", help="Operator reason (required with --reject)."
)
_RESUME_PROJECT_OPTION = typer.Option(
    None,
    "--project",
    help="Project path override (default: the path recorded in the run "
    "artifact).",
)


@app.command(
    help="Resume a paused run: show the pending approval, or resolve it "
    "with --approve / --reject --reason (docs/32)."
)
def resume(
    run_id: str = _RESUME_RUN_ID_ARG,
    approve: bool = _APPROVE_OPTION,
    reject: bool = _REJECT_OPTION,
    reason: str | None = _REASON_OPTION,
    project: Path | None = _RESUME_PROJECT_OPTION,
) -> None:
    from foundry.cli.resume import execute_resume

    raise typer.Exit(
        code=execute_resume(
            run_id,
            approve=approve,
            reject=reject,
            reason=reason,
            project=project,
        )
    )


approvals_app = typer.Typer(
    name="approvals",
    help="Pending HITL approvals across local runs.",
    no_args_is_help=True,
)
app.add_typer(approvals_app)

_APPROVALS_PROJECT_ARG = typer.Argument(
    None, help="Optional project name filter."
)


@approvals_app.command(
    name="list", help="List runs paused on a pending approval."
)
def approvals_list(project: str | None = _APPROVALS_PROJECT_ARG) -> None:
    from foundry.cli.resume import execute_approvals_list

    raise typer.Exit(code=execute_approvals_list(project))


connections_app = typer.Typer(
    name="connections",
    help="Inspect and health-check a project's bound connections.",
    no_args_is_help=True,
)
app.add_typer(connections_app)

_HEALTH_TARGET_ARG = typer.Argument(
    ...,
    help=(
        "Project dir, or <project-dir>/<connection-name> for one binding "
        "(e.g. projects/hello/time_service)."
    ),
)


@connections_app.command(
    name="health",
    help="Run the health.yaml eval for a project's bound connection(s).",
)
def connections_health(target: str = _HEALTH_TARGET_ARG) -> None:
    from foundry.cli.connections import execute_connections_health

    raise typer.Exit(code=execute_connections_health(target))


_FORGE_PROJECT_ARG = typer.Argument(
    ..., help="Project path (projects/qa_bot) or name (qa_bot)."
)
_FORGE_DESCRIPTION_OPTION = typer.Option(
    ...,
    "--description",
    help="What the system should do (the meta-agent's brief).",
)
_FORGE_EVAL_OPTION = typer.Option(
    ...,
    "--eval",
    help="Project-scope eval set path (the target; never modified).",
)
_FORGE_THRESHOLD_OPTION = typer.Option(
    0.9, "--threshold", help="Aggregate score the forge must reach."
)
_FORGE_MAX_ITER_OPTION = typer.Option(
    5, "--max-iter", help="Improvement iterations after bootstrap."
)
_FORGE_MAX_COST_OPTION = typer.Option(
    None,
    "--max-cost-usd",
    help="Forge-wide cost cap (meta-agent LLM spend + eval spend).",
)
_FORGE_MODEL_OPTION = typer.Option(
    None,
    "--model",
    help="Meta-agent model binding, '<provider>/<model>' "
    "(default anthropic/claude-opus-4-7).",
)
_FORGE_PLATEAU_OPTION = typer.Option(
    3,
    "--no-improvement-after",
    help="Stop after this many consecutive non-improving iterations.",
)
_FORGE_QUIET_OPTION = typer.Option(
    False, "--quiet", help="Suppress progress lines; print the summary only."
)


@app.command(
    help="Drive the meta-agent: bootstrap + eval-driven iteration until "
    "the threshold, a budget cap, or a plateau (docs/60)."
)
def forge(
    project: str = _FORGE_PROJECT_ARG,
    description: str = _FORGE_DESCRIPTION_OPTION,
    eval_path: str = _FORGE_EVAL_OPTION,
    threshold: float = _FORGE_THRESHOLD_OPTION,
    max_iter: int = _FORGE_MAX_ITER_OPTION,
    max_cost_usd: str | None = _FORGE_MAX_COST_OPTION,
    model: str | None = _FORGE_MODEL_OPTION,
    no_improvement_after: int = _FORGE_PLATEAU_OPTION,
    quiet: bool = _FORGE_QUIET_OPTION,
) -> None:
    from foundry.cli.forge import execute_forge

    raise typer.Exit(
        code=execute_forge(
            project,
            description=description,
            eval_path=eval_path,
            threshold=threshold,
            max_iter=max_iter,
            max_cost_usd=max_cost_usd,
            model=model,
            no_improvement_after=no_improvement_after,
            quiet=quiet,
        )
    )


project_app = typer.Typer(
    name="project",
    help="Project lifecycle. `new` is live (Phase 6).",
    no_args_is_help=True,
)
app.add_typer(project_app)

_PROJECT_NAME_ARG = typer.Argument(
    ..., help="Project name (becomes projects/<name> + branch foundry/<name>)."
)


@project_app.command(
    name="new",
    help="Create a project skeleton (evals/ + README) and its "
    "foundry/<name> branch. The meta-agent scaffolds the rest.",
)
def project_new(name: str = _PROJECT_NAME_ARG) -> None:
    from foundry.cli.project import execute_project_new

    raise typer.Exit(code=execute_project_new(name))


catalog_app = typer.Typer(
    name="catalog",
    help="Catalog operations. `promote` is live (Phase 5); list/show land "
    "in Phase 9.",
    no_args_is_help=True,
)
app.add_typer(catalog_app)

_PROMOTE_TARGET_ARG = typer.Argument(
    ...,
    help="<project>/<kind>/<name>, e.g. hello/tool/word_stats or "
    "hello/connection/time_api.",
)
_FLOOR_OPTION = typer.Option(
    0.85,
    "--floor",
    help="Minimum standalone-eval score (tool) / health score (connection) "
    "required to promote.",
)
_STRICT_SEMVER_OPTION = typer.Option(
    False,
    "--strict-semver",
    help="BLOCK schema-breaking promotions instead of warning (docs/50).",
)
_ALLOW_BREAKING_OPTION = typer.Option(
    False,
    "--allow-breaking",
    help="With --strict-semver: override the block for a breaking promotion.",
)
_YES_OPTION = typer.Option(
    False, "--yes", help="Skip interactive confirmation prompts."
)
_NOTES_OPTION = typer.Option(
    "", "--notes", help="Free-text 'why this version exists' for versions.json."
)


@catalog_app.command(
    name="promote",
    help="Promote a project-local tool/connection's latest version to the "
    "catalog (human-gated; eval-score floor enforced).",
)
def catalog_promote(
    target: str = _PROMOTE_TARGET_ARG,
    floor: float = _FLOOR_OPTION,
    strict_semver: bool = _STRICT_SEMVER_OPTION,
    allow_breaking: bool = _ALLOW_BREAKING_OPTION,
    yes: bool = _YES_OPTION,
    notes: str = _NOTES_OPTION,
) -> None:
    from foundry.cli.catalog import execute_catalog_promote

    raise typer.Exit(
        code=execute_catalog_promote(
            target,
            floor=floor,
            strict_semver=strict_semver,
            allow_breaking=allow_breaking,
            assume_yes=yes,
            notes=notes,
        )
    )


@catalog_app.command(name="list", help="List catalog artifacts. Lands in Phase 9.")
def catalog_list() -> None:
    _not_yet_implemented("catalog list", "Phase 9")


@catalog_app.command(
    name="show", help="Show an artifact's versions.json. Lands in Phase 9."
)
def catalog_show() -> None:
    _not_yet_implemented("catalog show", "Phase 9")


_EVAL_TARGET_ARG = typer.Argument(
    ...,
    help=(
        "Project path (run a project eval), or one of: 'tool', 'agent', "
        "'compare', 'show', 'list'."
    ),
)
_EVAL_ARGS = typer.Argument(
    None,
    help=(
        "Remaining positionals: <eval-set> after a project path; "
        "<ref>@<version> after 'tool'; <project> <agent> after 'agent'; "
        "versions after 'compare --tool'; <eval_run_id> after 'show'; "
        "<project> after 'list'."
    ),
)
_FAIL_UNDER_OPTION = typer.Option(
    None,
    "--fail-under",
    help="Exit non-zero when the aggregate score is below this floor (CI gate).",
)
_JSON_OPTION = typer.Option(
    False, "--json", help="Emit the full machine-readable result as JSON."
)
_COMPARE_TOOL_OPTION = typer.Option(
    None, "--tool", help="compare: tool name; versions follow as positionals."
)
_COMPARE_PROJECT_OPTION = typer.Option(
    None, "--project", help="compare: project path for pin-set comparison."
)
_PIN_SET_OPTION = typer.Option(
    None,
    "--pin-set",
    help="compare --project: git ref to materialize (repeatable; "
    "'worktree' = live tree).",
)
_EVAL_NAME_OPTION = typer.Option(
    None,
    "--eval",
    help="Eval set override: a path (tool/compare) or a name under the "
    "agent's eval/ dir.",
)


@app.command(
    name="eval",
    help="Run evals and compare across versions (docs/40 § CLI surface).",
)
def eval_(
    target: str = _EVAL_TARGET_ARG,
    args: list[str] | None = _EVAL_ARGS,
    fail_under: float | None = _FAIL_UNDER_OPTION,
    json_output: bool = _JSON_OPTION,
    tool: str | None = _COMPARE_TOOL_OPTION,
    project: str | None = _COMPARE_PROJECT_OPTION,
    pin_set: list[str] | None = _PIN_SET_OPTION,
    eval_name: str | None = _EVAL_NAME_OPTION,
) -> None:
    # Function suffixed with `_` to avoid shadowing the Python builtin;
    # the user-facing command is `foundry eval` via the decorator's `name=`.
    from foundry.cli.eval import execute_eval

    raise typer.Exit(
        code=execute_eval(
            target,
            list(args or []),
            fail_under=fail_under,
            json_output=json_output,
            tool=tool,
            project=project,
            pin_sets=list(pin_set or []),
            eval_option=eval_name,
        )
    )


_ROLLBACK_PROJECT_ARG = typer.Argument(
    ..., help="Project path (projects/hello) or name (hello)."
)
_ROLLBACK_TOOL_OPTION = typer.Option(
    None, "--tool", help="Roll back ONE tool pin in system.yaml."
)
_ROLLBACK_PROMPT_OPTION = typer.Option(
    None, "--prompt", help="Roll back ONE agent's prompt pin in agent.yaml."
)
_ROLLBACK_TO_OPTION = typer.Option(
    None,
    "--to",
    help="Target: v<N> with --tool/--prompt; a commit ref for a whole-"
    "project rollback.",
)
_FORCE_OPTION = typer.Option(
    False,
    "--force",
    help="Bypass force-able pre-flight checks (dirty tree, schema "
    "incompatibility). Logged loudly to the audit trail.",
)
_DRY_RUN_OPTION = typer.Option(
    False, "--dry-run", help="Show the plan + pre-flight results; change nothing."
)
_ROLLBACK_YES_OPTION = typer.Option(
    False, "--yes", help="Apply without the interactive confirmation prompt."
)


@app.command(
    help="Per-tool / per-prompt / per-project rollback with pre-flight "
    "checks (docs/52)."
)
def rollback(
    project: str = _ROLLBACK_PROJECT_ARG,
    tool: str | None = _ROLLBACK_TOOL_OPTION,
    prompt: str | None = _ROLLBACK_PROMPT_OPTION,
    to: str | None = _ROLLBACK_TO_OPTION,
    force: bool = _FORCE_OPTION,
    dry_run: bool = _DRY_RUN_OPTION,
    yes: bool = _ROLLBACK_YES_OPTION,
) -> None:
    from foundry.cli.rollback import execute_rollback_command

    raise typer.Exit(
        code=execute_rollback_command(
            project,
            tool=tool,
            prompt=prompt,
            to=to,
            force=force,
            dry_run=dry_run,
            assume_yes=yes,
        )
    )


_VERSIONS_TOOL_OPTION = typer.Option(
    None, "--tool", help="Show one tool's versions + pin only."
)


@app.command(
    help="Recent commits + per-artifact version state for a project "
    "(docs/52 § Listing versions)."
)
def versions(
    project: str = _ROLLBACK_PROJECT_ARG,
    tool: str | None = _VERSIONS_TOOL_OPTION,
) -> None:
    from foundry.cli.versions import execute_versions

    raise typer.Exit(code=execute_versions(project, tool=tool))


_DIFF_REF1_ARG = typer.Argument(..., help="Base ref (e.g. HEAD~3).")
_DIFF_REF2_ARG = typer.Argument(..., help="Target ref (e.g. HEAD).")
_DIFF_PATH_OPTION = typer.Option(
    None, "--path", help="Restrict the diff to a subtree/file of the project."
)


@app.command(
    name="diff",
    help="git diff between two refs, scoped to the project subtree.",
)
def diff_(
    project: str = _ROLLBACK_PROJECT_ARG,
    ref1: str = _DIFF_REF1_ARG,
    ref2: str = _DIFF_REF2_ARG,
    path: str | None = _DIFF_PATH_OPTION,
) -> None:
    from foundry.cli.versions import execute_diff

    raise typer.Exit(code=execute_diff(project, ref1, ref2, path=path))


_SERVE_HOST_OPTION = typer.Option("127.0.0.1", "--host", help="Bind address.")
_SERVE_PORT_OPTION = typer.Option(8000, "--port", help="Bind port.")
_SERVE_WORKERS_OPTION = typer.Option(
    1,
    "--workers",
    help="uvicorn worker processes. >1 requires --checkpoint sqlite "
    "(shared on one host); the multi-host prod shape is documented in "
    "docs/85.",
)
_SERVE_CHECKPOINT_OPTION = typer.Option(
    "sqlite",
    "--checkpoint",
    help="Checkpointer for served runs: 'sqlite' (default; enables "
    "kill+resume and HITL pauses), 'memory', or 'none'.",
)
_SERVE_PREFIX_OPTION = typer.Option(
    "",
    "--route-prefix",
    help="URL prefix for all routes (docs/70 versioning Pattern 2, "
    "e.g. /v1).",
)


@app.command(
    help="Serve a configured project as an auto-generated FastAPI app "
    "(docs/70): POST /run /stream /batch, WS /ws, run status + resume, "
    "health, config."
)
def serve(
    project_path: Path = _PROJECT_PATH_ARG,
    host: str = _SERVE_HOST_OPTION,
    port: int = _SERVE_PORT_OPTION,
    workers: int = _SERVE_WORKERS_OPTION,
    checkpoint: str = _SERVE_CHECKPOINT_OPTION,
    route_prefix: str = _SERVE_PREFIX_OPTION,
) -> None:
    from foundry.cli.serve import execute_serve

    raise typer.Exit(
        code=execute_serve(
            project_path,
            host=host,
            port=port,
            workers=workers,
            checkpoint=checkpoint,
            route_prefix=route_prefix,
        )
    )


@app.command(help="Query observability store: cost, p95, failures. Lands in Phase 9.")
def obs() -> None:
    _not_yet_implemented("obs", "Phase 9")


def main() -> None:
    """Console-script entry point referenced from pyproject.toml."""
    app()


if __name__ == "__main__":
    sys.exit(app() or 0)
