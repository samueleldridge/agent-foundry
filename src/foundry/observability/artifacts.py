"""Run-artifact writer: the audit trail every run leaves on disk.

Layout (Phase 2a subset of the full Phase 9 spec in docs/81):

    ~/.foundry/runs/<run_id>/
    ├── metadata.json      run_id, project, status, provider/model, budget,
    │                      pins (tool + connection versions), pool metrics
    ├── events.jsonl       every RunEvent, in sequence order (incl.
    │                      connection lifecycle events)
    ├── llm_calls.jsonl    one record per LLM call (token_usage incl.
    │                      reasoning_tokens, cost, latency, stop_reason)
    └── tool_calls.jsonl   one record per tool dispatch (ref, version,
                           success, latency, retries, error_category)

Never writes secrets: events carry no credentials by construction
(ConnectionDescriptor is redacted at build time), and metadata is assembled
from spec fields only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from foundry.core import (
    LLMCallCompleted,
    LLMCallStarted,
    RunCompleted,
    RunId,
    StateTransition,
    ToolCompleted,
    ToolStarted,
)
from foundry.storage.paths import run_dir


def _jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(v) for v in value]
    return value


class RunArtifactWriter:
    """Collects RunEvents and writes the per-run artifact directory."""

    def __init__(self, run_id: RunId, directory: Path | None = None) -> None:
        self.run_id = run_id
        self.directory = directory or run_dir(str(run_id))
        self.directory.mkdir(parents=True, exist_ok=True)
        self._events_path = self.directory / "events.jsonl"
        self._llm_calls_path = self.directory / "llm_calls.jsonl"
        self._tool_calls_path = self.directory / "tool_calls.jsonl"
        self._state_transitions_path = self.directory / "state_transitions.jsonl"
        self._last_llm_started: LLMCallStarted | None = None
        self._last_tool_started: ToolStarted | None = None

    def next_sequence(self) -> int:
        """The next RunEvent sequence number for this run: a resumed run
        appends to events.jsonl and continues the sequence where the killed
        process stopped (event-stream invariant 1 across processes).

        A SIGKILL mid-write can leave a torn trailing line (no ``\\n``);
        counting it would both miscount the sequence and leave garbage the
        next append glues onto — so the partial line is truncated away and
        only complete lines count (Phase 3 review finding 3)."""
        if not self._events_path.exists():
            return 0
        raw = self._events_path.read_bytes()
        if not raw:
            return 0
        complete, newline, partial = raw.rpartition(b"\n")
        if partial:
            with self._events_path.open("wb") as fh:
                fh.write(complete + newline)
        if not newline:
            return 0
        return complete.count(b"\n") + 1

    def record_event(self, event: BaseModel) -> None:
        with self._events_path.open("a") as fh:
            fh.write(event.model_dump_json() + "\n")
        if isinstance(event, LLMCallStarted):
            self._last_llm_started = event
        elif isinstance(event, LLMCallCompleted):
            self._record_llm_call(event)
        elif isinstance(event, ToolStarted):
            self._last_tool_started = event
        elif isinstance(event, ToolCompleted):
            self._record_tool_call(event)
        elif isinstance(event, StateTransition):
            # docs/81: state mutations get their own stream alongside the
            # full event log (per-run debugging surface).
            with self._state_transitions_path.open("a") as fh:
                fh.write(event.model_dump_json() + "\n")
        elif isinstance(event, RunCompleted) and event.final_output is not None:
            self.write_outputs(event.final_output)

    def _record_llm_call(self, event: LLMCallCompleted) -> None:
        started = self._last_llm_started
        record: dict[str, Any] = {
            "run_id": str(event.run_id),
            "agent_name": event.agent_name,
            "provider": started.provider if started else None,
            "model": started.model if started else None,
            "prompt_messages": (
                [m.model_dump(mode="json") for m in started.prompt_messages]
                if started is not None and started.prompt_messages is not None
                else None
            ),
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

    def _record_tool_call(self, event: ToolCompleted) -> None:
        started = self._last_tool_started
        record: dict[str, Any] = {
            "run_id": str(event.run_id),
            "agent_name": event.agent_name,
            "tool_ref": event.tool_ref,
            "tool_version": event.tool_version,
            "input_hash": (
                started.input_hash
                if started is not None and started.tool_ref == event.tool_ref
                else None
            ),
            "success": event.success,
            "latency_ms": event.latency_ms,
            "retry_count": event.retry_count,
            "error_category": event.error_category,
            "timestamp": event.timestamp.isoformat(),
        }
        with self._tool_calls_path.open("a") as fh:
            fh.write(json.dumps(record) + "\n")

    def write_inputs(self, inputs: dict[str, Any]) -> None:
        """Persist the request input (docs/81 inputs.json). Callers honour
        ObservabilityConfig.capture_inputs before calling."""
        (self.directory / "inputs.json").write_text(
            json.dumps({"inputs": _jsonable(inputs)}, indent=2, default=str) + "\n"
        )

    def write_outputs(self, output: Any) -> None:
        """Persist the final output (docs/81 outputs.json); called from the
        run.completed event so every terminal run leaves one."""
        (self.directory / "outputs.json").write_text(
            json.dumps({"output": _jsonable(output)}, indent=2, default=str) + "\n"
        )

    def write_final_state(self, state: dict[str, Any]) -> None:
        """Persist the run's final state projection (final_state.json) —
        the debug surface for memory fields + function-node pipelines."""
        (self.directory / "final_state.json").write_text(
            json.dumps({"state": _jsonable(state)}, indent=2, default=str) + "\n"
        )

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
