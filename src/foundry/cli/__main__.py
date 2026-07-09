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
        "`foundry run` (Phase 1), `foundry connections health` (Phase 2a) "
        "and `foundry eval` (Phase 4) are live. Remaining subcommands land "
        "in later phases; see docs/03-development-phases.md."
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


@app.command(help="Drive the meta-agent against a project. Lands in Phase 6.")
def forge() -> None:
    _not_yet_implemented("forge", "Phase 6")


@app.command(help="Project lifecycle: new / list / diff. Lands in Phase 6.")
def project() -> None:
    _not_yet_implemented("project", "Phase 6")


@app.command(help="Catalog operations: list / show / promote. Lands in Phase 5.")
def catalog() -> None:
    _not_yet_implemented("catalog", "Phase 5")


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


@app.command(help="Per-artifact and per-project rollback. Lands in Phase 5.")
def rollback() -> None:
    _not_yet_implemented("rollback", "Phase 5")


@app.command(help="Serve a configured project as a FastAPI app. Lands in Phase 8.")
def serve() -> None:
    _not_yet_implemented("serve", "Phase 8")


@app.command(help="Query observability store: cost, p95, failures. Lands in Phase 9.")
def obs() -> None:
    _not_yet_implemented("obs", "Phase 9")


def main() -> None:
    """Console-script entry point referenced from pyproject.toml."""
    app()


if __name__ == "__main__":
    sys.exit(app() or 0)
