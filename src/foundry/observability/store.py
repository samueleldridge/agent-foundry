"""Local SQLite event-mirror (docs/80 § transport 3).

Mirrors the same RunEvent stream that feeds OTel into
``~/.foundry/observability.db`` (``FOUNDRY_HOME`` override honoured) so
``foundry obs`` can answer cross-run questions without an OTel collector.

Schema (docs/80 § Local SQLite event-mirror): ``runs``, ``llm_calls``,
``tool_calls``, ``handoffs``, ``evals`` + a ``schema_meta`` version row.
The store is per-worker state (docs/81 § Multi-host): each process appends
to the DB under its own ``FOUNDRY_HOME``; the OTel stream is the
cross-worker source of truth.

Failure policy (docs/80 § Failure modes): the store must never take a run
down. Callers go through :mod:`foundry.observability.events`, which wraps
every handler in a degradation guard.
"""

from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from foundry.core.errors import ConfigError
from foundry.core.events import (
    Handoff,
    LLMCallCompleted,
    LLMCallStarted,
    RunCancelledEvent,
    RunCompleted,
    RunFailed,
    RunStarted,
    ToolCompleted,
)
from foundry.storage.paths import foundry_home

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    schema_version INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    system_version TEXT NOT NULL DEFAULT '',
    worker_id TEXT NOT NULL DEFAULT '',
    started_at TEXT,
    completed_at TEXT,
    status TEXT NOT NULL DEFAULT 'in_progress',
    total_input_tokens INTEGER NOT NULL DEFAULT 0,
    total_output_tokens INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    error_class TEXT
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER NOT NULL DEFAULT 0,
    cached_read_tokens INTEGER NOT NULL DEFAULT 0,
    cached_write_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    stop_reason TEXT NOT NULL DEFAULT '',
    timestamp TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    agent TEXT NOT NULL DEFAULT '',
    tool_ref TEXT NOT NULL,
    tool_version TEXT NOT NULL DEFAULT '',
    success INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_category TEXT,
    timestamp TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS handoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    trigger TEXT NOT NULL,
    hop_number INTEGER NOT NULL DEFAULT 0,
    timestamp TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS evals (
    eval_run_id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    eval_name TEXT NOT NULL DEFAULT '',
    target_ref TEXT NOT NULL DEFAULT '',
    target_version TEXT NOT NULL DEFAULT '',
    eval_spec_hash TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL,
    threshold REAL NOT NULL,
    passed INTEGER NOT NULL,
    cases_total INTEGER NOT NULL DEFAULT 0,
    cases_passed INTEGER NOT NULL DEFAULT 0,
    cost_total_usd REAL,
    completed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_project_ts ON llm_calls (project, timestamp);
CREATE INDEX IF NOT EXISTS idx_tool_calls_ref ON tool_calls (tool_ref, timestamp);
CREATE INDEX IF NOT EXISTS idx_handoffs_run ON handoffs (run_id);
CREATE INDEX IF NOT EXISTS idx_runs_project ON runs (project, started_at);
"""

_SINCE_RE = re.compile(r"^(\d+)([dhm])$")


def observability_db_path() -> Path:
    """``<FOUNDRY_HOME>/observability.db`` (docs/80)."""
    return foundry_home() / "observability.db"


def parse_since(value: str, *, now: datetime | None = None) -> datetime:
    """Parse a ``--since`` duration (``7d`` / ``24h`` / ``30m``) into the
    UTC cutoff datetime it denotes."""
    match = _SINCE_RE.match(value.strip())
    if match is None:
        raise ConfigError(
            f"invalid --since duration {value!r}: expected <N>d, <N>h, or <N>m",
            context={"value": value},
        )
    amount = int(match.group(1))
    unit = match.group(2)
    delta = {
        "d": timedelta(days=amount),
        "h": timedelta(hours=amount),
        "m": timedelta(minutes=amount),
    }[unit]
    return (now or datetime.now(UTC)) - delta


def _cost_to_float(cost: Decimal | None) -> float | None:
    return float(cost) if cost is not None else None


@dataclass
class _RunState:
    """Per-run correlation the mirror needs across events: llm.completed
    carries no provider/model, so the last llm.started per agent supplies
    them (same derivation the RunArtifactWriter uses)."""

    project: str = ""
    system_version: str = ""
    llm_started: dict[str, LLMCallStarted] = field(default_factory=dict)


class ObservabilityStore:
    """SQLite mirror of the RunEvent stream + the ``foundry obs`` query
    surface. One instance per DB path; safe for multi-threaded writers."""

    def __init__(self, db_path: Path | None = None) -> None:
        self.db_path = db_path or observability_db_path()
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._runs: dict[str, _RunState] = {}

    # -- connection -----------------------------------------------------

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.db_path, check_same_thread=False)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=5000")
            conn.executescript(_SCHEMA)
            row = conn.execute("SELECT schema_version FROM schema_meta").fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO schema_meta (schema_version) VALUES (?)",
                    (SCHEMA_VERSION,),
                )
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # -- event mirror ----------------------------------------------------

    def record_event(self, event: BaseModel) -> None:
        """Mirror one RunEvent. Only the docs/80 table-relevant kinds write
        rows; everything else is a no-op (the full stream lives in the run
        artifact's events.jsonl)."""
        if isinstance(event, RunStarted):
            self._on_run_started(event)
        elif isinstance(event, LLMCallStarted):
            state = self._state(str(event.run_id))
            state.llm_started[event.agent_name] = event
        elif isinstance(event, LLMCallCompleted):
            self._on_llm_completed(event)
        elif isinstance(event, RunCompleted):
            self._on_run_terminal(
                event,
                status=event.status,
                totals=(
                    event.total_input_tokens,
                    event.total_output_tokens,
                    _cost_to_float(event.total_cost_estimate_usd),
                    event.duration_ms,
                ),
            )
        elif isinstance(event, RunFailed):
            self._on_run_terminal(
                event,
                status="failed",
                error_class=str(event.error.get("error", event.error.get("type", ""))),
            )
        elif isinstance(event, RunCancelledEvent):
            self._on_run_terminal(event, status="cancelled", error_class=event.reason)
        elif isinstance(event, Handoff):
            self._on_handoff(event)
        elif isinstance(event, ToolCompleted):
            self._on_tool_completed(event)

    def _state(self, run_id: str) -> _RunState:
        return self._runs.setdefault(run_id, _RunState())

    def _on_run_started(self, event: RunStarted) -> None:
        state = self._state(str(event.run_id))
        state.project = event.project
        state.system_version = event.system_version
        with self._lock:
            conn = self._connection()
            conn.execute(
                "INSERT OR REPLACE INTO runs "
                "(run_id, project, system_version, worker_id, started_at, status) "
                "VALUES (?, ?, ?, ?, ?, 'in_progress')",
                (
                    str(event.run_id),
                    event.project,
                    event.system_version,
                    event.worker_id,
                    event.timestamp.isoformat(),
                ),
            )
            conn.commit()

    def _on_llm_completed(self, event: LLMCallCompleted) -> None:
        state = self._state(str(event.run_id))
        started = state.llm_started.get(event.agent_name)
        with self._lock:
            conn = self._connection()
            conn.execute(
                "INSERT INTO llm_calls (run_id, project, agent, provider, model, "
                "input_tokens, output_tokens, reasoning_tokens, cached_read_tokens, "
                "cached_write_tokens, cost_usd, latency_ms, stop_reason, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(event.run_id),
                    state.project,
                    event.agent_name,
                    started.provider if started else "",
                    started.model if started else "",
                    event.usage.input_tokens,
                    event.usage.output_tokens,
                    event.usage.reasoning_tokens,
                    event.usage.cached_read_tokens,
                    event.usage.cached_write_tokens,
                    _cost_to_float(event.cost_estimate_usd),
                    event.latency_ms,
                    event.stop_reason.value,
                    event.timestamp.isoformat(),
                ),
            )
            conn.commit()

    def _on_tool_completed(self, event: ToolCompleted) -> None:
        state = self._state(str(event.run_id))
        with self._lock:
            conn = self._connection()
            conn.execute(
                "INSERT INTO tool_calls (run_id, project, agent, tool_ref, tool_version, "
                "success, latency_ms, retry_count, error_category, timestamp) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    str(event.run_id),
                    state.project,
                    event.agent_name,
                    event.tool_ref,
                    event.tool_version,
                    1 if event.success else 0,
                    event.latency_ms,
                    event.retry_count,
                    event.error_category,
                    event.timestamp.isoformat(),
                ),
            )
            conn.commit()

    def _on_handoff(self, event: Handoff) -> None:
        state = self._state(str(event.run_id))
        with self._lock:
            conn = self._connection()
            conn.execute(
                "INSERT INTO handoffs (run_id, project, from_agent, to_agent, trigger, "
                "hop_number, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    str(event.run_id),
                    state.project,
                    event.from_agent,
                    event.to_agent,
                    event.trigger,
                    event.hop_number,
                    event.timestamp.isoformat(),
                ),
            )
            conn.commit()

    def _on_run_terminal(
        self,
        event: RunCompleted | RunFailed | RunCancelledEvent,
        *,
        status: str,
        totals: tuple[int, int, float | None, int] | None = None,
        error_class: str | None = None,
    ) -> None:
        run_id = str(event.run_id)
        timestamp = event.timestamp
        with self._lock:
            conn = self._connection()
            conn.execute(
                "INSERT OR IGNORE INTO runs (run_id, status) VALUES (?, 'in_progress')",
                (run_id,),
            )
            if totals is not None:
                conn.execute(
                    "UPDATE runs SET status=?, completed_at=?, total_input_tokens=?, "
                    "total_output_tokens=?, total_cost_usd=?, duration_ms=? WHERE run_id=?",
                    (
                        status,
                        timestamp.isoformat(),
                        totals[0],
                        totals[1],
                        totals[2],
                        totals[3],
                        run_id,
                    ),
                )
            else:
                conn.execute(
                    "UPDATE runs SET status=?, completed_at=?, error_class=? WHERE run_id=?",
                    (status, timestamp.isoformat(), error_class, run_id),
                )
            conn.commit()
        self._runs.pop(run_id, None)

    # -- eval mirror (called by the eval harness; kwargs avoid an import
    # cycle with foundry.eval) --------------------------------------------

    def record_eval(
        self,
        *,
        eval_run_id: str,
        project: str,
        eval_name: str,
        target_ref: str,
        target_version: str,
        eval_spec_hash: str,
        score: float,
        threshold: float,
        passed: bool,
        cases_total: int,
        cases_passed: int,
        cost_total_usd: float | None,
        completed_at: str,
    ) -> None:
        with self._lock:
            conn = self._connection()
            conn.execute(
                "INSERT OR REPLACE INTO evals (eval_run_id, project, eval_name, target_ref, "
                "target_version, eval_spec_hash, score, threshold, passed, cases_total, "
                "cases_passed, cost_total_usd, completed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    eval_run_id,
                    project,
                    eval_name,
                    target_ref,
                    target_version,
                    eval_spec_hash,
                    score,
                    threshold,
                    1 if passed else 0,
                    cases_total,
                    cases_passed,
                    cost_total_usd,
                    completed_at,
                ),
            )
            conn.commit()

    # -- query surface (foundry obs) --------------------------------------

    def cost_breakdown(
        self,
        *,
        project: str | None = None,
        since: datetime | None = None,
        by: str = "model",
    ) -> list[dict[str, Any]]:
        """Aggregate llm_calls cost. ``by`` is one of model / day / agent."""
        group = {
            "model": "provider || ':' || model",
            "day": "substr(timestamp, 1, 10)",
            "agent": "agent",
        }.get(by)
        if group is None:
            raise ConfigError(f"invalid --by {by!r}: expected model, day, or agent")
        where, params = self._filters(project=project, since=since)
        sql = (
            # group is a fixed expression from the mapping above, never user input
            f"SELECT {group} AS bucket, COUNT(*) AS calls, "
            "SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens, "
            "SUM(COALESCE(cost_usd, 0.0)) AS cost_usd "
            f"FROM llm_calls {where} GROUP BY bucket ORDER BY cost_usd DESC"
        )
        with self._lock:
            rows = self._connection().execute(sql, params).fetchall()
        return [
            {
                "bucket": row[0],
                "calls": row[1],
                "input_tokens": row[2] or 0,
                "output_tokens": row[3] or 0,
                "cost_usd": round(row[4] or 0.0, 6),
            }
            for row in rows
        ]

    def total_cost(
        self, *, project: str | None = None, since: datetime | None = None
    ) -> float:
        where, params = self._filters(project=project, since=since)
        sql = f"SELECT SUM(COALESCE(cost_usd, 0.0)) FROM llm_calls {where}"
        with self._lock:
            row = self._connection().execute(sql, params).fetchone()
        return round(row[0] or 0.0, 6)

    def tool_failures(
        self,
        *,
        tool_ref: str | None = None,
        project: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if tool_ref:
            clauses.append("tool_ref = ?")
            params.append(tool_ref)
        if project:
            clauses.append("project = ?")
            params.append(project)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT tool_ref, tool_version, COUNT(*) AS calls, "
            "SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) AS failures, "
            "MAX(CASE WHEN success = 0 THEN COALESCE(error_category, '') ELSE '' END) "
            "AS last_error_category "
            f"FROM tool_calls {where} GROUP BY tool_ref, tool_version "
            "ORDER BY failures DESC, calls DESC"
        )
        with self._lock:
            rows = self._connection().execute(sql, params).fetchall()
        return [
            {
                "tool_ref": row[0],
                "tool_version": row[1],
                "calls": row[2],
                "failures": row[3],
                "failure_rate": round(row[3] / row[2], 4) if row[2] else 0.0,
                "last_error_category": row[4] or None,
            }
            for row in rows
        ]

    def latency_percentiles(
        self,
        *,
        model: str | None = None,
        project: str | None = None,
        since: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Per-(provider, model) p50/p95 over llm_calls latencies."""
        clauses: list[str] = []
        params: list[Any] = []
        if model:
            clauses.append("model = ?")
            params.append(model)
        if project:
            clauses.append("project = ?")
            params.append(project)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"SELECT provider, model, latency_ms FROM llm_calls {where}"
        with self._lock:
            rows = self._connection().execute(sql, params).fetchall()
        grouped: dict[tuple[str, str], list[int]] = {}
        for provider, model_name, latency in rows:
            grouped.setdefault((provider, model_name), []).append(int(latency))
        out: list[dict[str, Any]] = []
        for (provider, model_name), latencies in sorted(grouped.items()):
            latencies.sort()
            out.append(
                {
                    "provider": provider,
                    "model": model_name,
                    "calls": len(latencies),
                    "p50_ms": _percentile(latencies, 50),
                    "p95_ms": _percentile(latencies, 95),
                }
            )
        return out

    def recent_runs(
        self,
        *,
        project: str | None = None,
        since: datetime | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if since:
            clauses.append("started_at >= ?")
            params.append(since.isoformat())
        if status:
            clauses.append("status = ?")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT run_id, project, status, started_at, completed_at, "
            "total_input_tokens, total_output_tokens, total_cost_usd, duration_ms "
            f"FROM runs {where} ORDER BY started_at DESC LIMIT ?"
        )
        params.append(limit)
        with self._lock:
            rows = self._connection().execute(sql, params).fetchall()
        keys = (
            "run_id",
            "project",
            "status",
            "started_at",
            "completed_at",
            "total_input_tokens",
            "total_output_tokens",
            "total_cost_usd",
            "duration_ms",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    def eval_rows(
        self, *, project: str | None = None, since: datetime | None = None
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if since:
            clauses.append("completed_at >= ?")
            params.append(since.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            "SELECT eval_run_id, project, eval_name, target_ref, score, threshold, "
            f"passed, cases_total, cases_passed, completed_at FROM evals {where} "
            "ORDER BY completed_at DESC"
        )
        with self._lock:
            rows = self._connection().execute(sql, params).fetchall()
        keys = (
            "eval_run_id",
            "project",
            "eval_name",
            "target_ref",
            "score",
            "threshold",
            "passed",
            "cases_total",
            "cases_passed",
            "completed_at",
        )
        return [dict(zip(keys, row, strict=True)) for row in rows]

    @staticmethod
    def _filters(
        *, project: str | None, since: datetime | None
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if project:
            clauses.append("project = ?")
            params.append(project)
        if since:
            clauses.append("timestamp >= ?")
            params.append(since.isoformat())
        return (f"WHERE {' AND '.join(clauses)}" if clauses else ""), params


def _percentile(sorted_values: list[int], pct: int) -> int:
    if not sorted_values:
        return 0
    index = min(len(sorted_values) - 1, max(0, round(pct / 100 * len(sorted_values)) - 1))
    return sorted_values[index]


__all__ = [
    "SCHEMA_VERSION",
    "ObservabilityStore",
    "observability_db_path",
    "parse_since",
]
