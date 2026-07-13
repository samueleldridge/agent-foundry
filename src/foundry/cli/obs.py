"""`foundry obs` — query the local SQLite event-mirror (docs/80 § CLI).

Every subcommand reads ``<FOUNDRY_HOME>/observability.db`` (invariant 6:
``foundry obs`` queries the mirror, not the OTel backend). Tabular by
default; ``--json`` for machine-readable output. Exit codes: 0 results
printed (possibly empty), 2 config/infrastructure error.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from foundry.cli._helpers import print_foundry_error
from foundry.core.errors import FoundryError
from foundry.observability.events import get_store
from foundry.observability.store import parse_since


def _since(value: str | None) -> datetime | None:
    return parse_since(value) if value else None


def _print_table(rows: list[dict[str, Any]], columns: list[tuple[str, str]]) -> None:
    """``columns`` is (key, header) pairs; widths fit the data."""
    widths = {
        key: max(len(header), *(len(str(row.get(key, ""))) for row in rows))
        for key, header in columns
    }
    header_line = "  ".join(header.ljust(widths[key]) for key, header in columns)
    print(header_line)
    print("-" * len(header_line))
    for row in rows:
        print("  ".join(str(row.get(key, "")).ljust(widths[key]) for key, _ in columns))


def execute_cost(
    *,
    project: str | None,
    since: str | None,
    by: str,
    json_output: bool,
) -> int:
    """Cost breakdown from llm_calls (docs/80 § Cost attribution)."""
    try:
        store = get_store()
        cutoff = _since(since)
        rows = store.cost_breakdown(project=project, since=cutoff, by=by)
        total = store.total_cost(project=project, since=cutoff)
        if json_output:
            print(json.dumps({"by": by, "rows": rows, "total_cost_usd": total}, indent=2))
            return 0
        scope = f"project {project}" if project else "all projects"
        window = f" since {since}" if since else ""
        print(f"cost breakdown — {scope}{window}, by {by}\n")
        if not rows:
            print("(no llm calls recorded)")
            return 0
        _print_table(
            rows,
            [
                ("bucket", by),
                ("calls", "calls"),
                ("input_tokens", "input_tok"),
                ("output_tokens", "output_tok"),
                ("cost_usd", "cost_usd"),
            ],
        )
        print(f"\ntotal: ${total:.6f}")
        return 0
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2


def execute_tool_failures(
    *,
    tool: str | None,
    project: str | None,
    since: str | None,
    json_output: bool,
) -> int:
    try:
        rows = get_store().tool_failures(
            tool_ref=tool, project=project, since=_since(since)
        )
        if json_output:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("(no tool calls recorded)")
            return 0
        _print_table(
            rows,
            [
                ("tool_ref", "tool_ref"),
                ("tool_version", "version"),
                ("calls", "calls"),
                ("failures", "failures"),
                ("failure_rate", "failure_rate"),
                ("last_error_category", "last_error"),
            ],
        )
        return 0
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2


def execute_p95(
    *,
    model: str | None,
    project: str | None,
    since: str | None,
    json_output: bool,
) -> int:
    try:
        rows = get_store().latency_percentiles(
            model=model, project=project, since=_since(since)
        )
        if json_output:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("(no llm calls recorded)")
            return 0
        _print_table(
            rows,
            [
                ("provider", "provider"),
                ("model", "model"),
                ("calls", "calls"),
                ("p50_ms", "p50_ms"),
                ("p95_ms", "p95_ms"),
            ],
        )
        return 0
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2


def execute_runs(
    *,
    project: str | None,
    since: str | None,
    status: str | None,
    json_output: bool,
) -> int:
    try:
        rows = get_store().recent_runs(
            project=project, since=_since(since), status=status
        )
        if json_output:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("(no runs recorded)")
            return 0
        _print_table(
            rows,
            [
                ("run_id", "run_id"),
                ("project", "project"),
                ("status", "status"),
                ("started_at", "started_at"),
                ("duration_ms", "ms"),
                ("total_cost_usd", "cost_usd"),
            ],
        )
        return 0
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2


def execute_eval_trend(
    *,
    project: str | None,
    since: str | None,
    json_output: bool,
) -> int:
    try:
        rows = get_store().eval_rows(project=project, since=_since(since))
        if json_output:
            print(json.dumps(rows, indent=2))
            return 0
        if not rows:
            print("(no evals recorded)")
            return 0
        _print_table(
            rows,
            [
                ("completed_at", "completed_at"),
                ("project", "project"),
                ("eval_name", "eval"),
                ("target_ref", "target"),
                ("score", "score"),
                ("passed", "passed"),
            ],
        )
        return 0
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2


__all__ = [
    "execute_cost",
    "execute_eval_trend",
    "execute_p95",
    "execute_runs",
    "execute_tool_failures",
]
