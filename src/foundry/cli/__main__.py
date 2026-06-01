"""`python -m foundry` entry point.

Phase 0 deliberately ships only `--help`. Every subcommand below is registered
as a placeholder that exits with a "not yet implemented" message and points at
the phase that will land it. See docs/03-development-phases.md.

CLI framework: Typer (chosen over argparse for typed signatures and over click
for the lighter decorator surface; documented in pyproject.toml).
"""

from __future__ import annotations

import sys
from typing import NoReturn

import typer

app = typer.Typer(
    name="foundry",
    help=(
        "agent-foundry — build, evaluate, version, and orchestrate "
        "multi-agent LLM systems from declarative configs.\n\n"
        "Phase 0 ships only `--help`. Subcommands land in later phases; "
        "see docs/03-development-phases.md."
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


@app.command(help="Run a configured system end-to-end. Lands in Phase 1.")
def run() -> None:
    _not_yet_implemented("run", "Phase 1")


@app.command(help="Drive the meta-agent against a project. Lands in Phase 6.")
def forge() -> None:
    _not_yet_implemented("forge", "Phase 6")


@app.command(help="Project lifecycle: new / list / diff. Lands in Phase 6.")
def project() -> None:
    _not_yet_implemented("project", "Phase 6")


@app.command(help="Catalog operations: list / show / promote. Lands in Phase 5.")
def catalog() -> None:
    _not_yet_implemented("catalog", "Phase 5")


@app.command(name="eval", help="Run evals and compare across versions. Lands in Phase 4.")
def eval_() -> None:
    # Function suffixed with `_` to avoid shadowing the Python builtin;
    # the user-facing command is `foundry eval` via the decorator's `name=`.
    _not_yet_implemented("eval", "Phase 4")


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
