"""Lightweight review TUI (docs/52 § Review TUI; Phase 9).

Built on ``rich`` (typer's dependency), not textual — see
:mod:`foundry.cli.tui.review` for the dependency decision.
"""

from foundry.cli.tui.review import (
    ReviewModel,
    execute_review,
    run_review_loop,
    screen_text,
)

__all__ = ["ReviewModel", "execute_review", "run_review_loop", "screen_text"]
