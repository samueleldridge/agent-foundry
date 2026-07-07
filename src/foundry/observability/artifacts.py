"""Run-artifact writer: the audit trail every run leaves on disk.

Layout (Phase 1 subset of the full Phase 9 spec in docs/81):

    ~/.foundry/runs/<run_id>/
    ├── metadata.json      run_id, project, status, provider/model, budget
    ├── events.jsonl       every RunEvent, in sequence order
    └── llm_calls.jsonl    one record per LLM call (token_usage incl.
                           reasoning_tokens, cost, latency, stop_reason)

Never writes secrets: events carry no credentials by construction, and
metadata is assembled from spec fields only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from foundry.core import LLMCallCompleted, LLMCallStarted, RunId
from foundry.storage.paths import run_dir


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return value


class RunArtifactWriter:
    """Collects RunEvents and writes the per-run artifact directory."""

    def __init__(self, run_id: RunId, directory: Path | None = None) -> None:
        self.run_id = run_id
        self.directory = directory or run_dir(str(run_id))
        self.directory.mkdir(parents=True, exist_ok=True)
        self._events_path = self.directory / "events.jsonl"
        self._llm_calls_path = self.directory / "llm_calls.jsonl"
        self._last_llm_started: LLMCallStarted | None = None

    def record_event(self, event: BaseModel) -> None:
        with self._events_path.open("a") as fh:
            fh.write(event.model_dump_json() + "\n")
        if isinstance(event, LLMCallStarted):
            self._last_llm_started = event
        elif isinstance(event, LLMCallCompleted):
            self._record_llm_call(event)

    def _record_llm_call(self, event: LLMCallCompleted) -> None:
        started = self._last_llm_started
        record: dict[str, Any] = {
            "run_id": str(event.run_id),
            "agent_name": event.agent_name,
            "provider": started.provider if started else None,
            "model": started.model if started else None,
            "token_usage": event.usage.model_dump(mode="json"),
            "cost_estimate_usd": (
                str(event.cost_estimate_usd)
                if event.cost_estimate_usd is not None
                else None
            ),
            "latency_ms": event.latency_ms,
            "stop_reason": event.stop_reason.value,
            "timestamp": event.timestamp.isoformat(),
        }
        with self._llm_calls_path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")

    def write_metadata(
        self,
        *,
        project: str,
        status: str,
        provider: str | None = None,
        model: str | None = None,
        error: dict[str, Any] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        metadata: dict[str, Any] = {
            "run_id": str(self.run_id),
            "project": project,
            "status": status,
            "provider": provider,
            "model": model,
            "written_at": datetime.now(UTC).isoformat(),
        }
        if error is not None:
            metadata["error"] = _jsonable(error)
        if extra:
            metadata.update({k: _jsonable(v) for k, v in extra.items()})
        (self.directory / "metadata.json").write_text(
            json.dumps(metadata, indent=2, default=str) + "\n"
        )


__all__ = ["RunArtifactWriter"]
