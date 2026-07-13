"""OTel metrics catalogue (docs/80 § transport 2).

Counters / histograms / gauges derived from the same RunEvent stream that
feeds traces and the SQLite mirror (docs/80 invariant 4: the three
transports stay consistent). Instruments are created against the global
``MeterProvider``; with no SDK installed every recording is a no-op, so
this module is always safe to call.

Dimension notes (deviations from the docs/80 table, both additive):

- ``foundry.run.input_tokens`` / ``output_tokens`` / ``reasoning_tokens``
  are recorded per ``llm.completed`` with ``project`` + ``provider`` +
  ``model`` dims (the run-level event has no provider/model); summing over
  a run's calls yields the documented run totals.
- ``foundry.run.cost_usd`` is recorded on ``run.completed`` with
  ``project`` + ``worker_id`` dims; the per-model cost mix lives on
  ``foundry.llm.cost_usd`` (``project``, ``provider``, ``model``).

Cost is recorded as float — indicative, not authoritative (docs/80 § Cost
attribution).
"""

from __future__ import annotations

from decimal import Decimal

from opentelemetry import metrics
from pydantic import BaseModel

from foundry.core.events import (
    EmbedCall,
    Handoff,
    LLMCallCompleted,
    RunCancelledEvent,
    RunCompleted,
    RunFailed,
    SemanticCacheHitEvent,
    ToolCompleted,
)

_METER_NAME = "foundry"


def _cost(value: Decimal | None) -> float:
    return float(value) if value is not None else 0.0


class MetricsRecorder:
    """Event → instrument recording. One instance per process (module
    singleton below); instruments resolve against the global MeterProvider
    lazily so a provider installed after import (tests, serve startup) is
    honoured."""

    def __init__(self) -> None:
        meter = metrics.get_meter(_METER_NAME)
        self._run_total = meter.create_counter(
            "foundry.run.total", description="Completed foundry runs by status."
        )
        self._run_duration = meter.create_histogram(
            "foundry.run.duration_ms", unit="ms", description="Run wall time."
        )
        self._run_cost = meter.create_counter(
            "foundry.run.cost_usd", unit="usd", description="Run cost estimate."
        )
        self._run_input_tokens = meter.create_counter(
            "foundry.run.input_tokens", description="Input tokens by project/provider/model."
        )
        self._run_output_tokens = meter.create_counter(
            "foundry.run.output_tokens", description="Output tokens by project/provider/model."
        )
        self._run_reasoning_tokens = meter.create_counter(
            "foundry.run.reasoning_tokens",
            description="Reasoning tokens by project/provider/model.",
        )
        self._llm_calls = meter.create_counter(
            "foundry.llm.calls_total", description="LLM calls by provider/model/agent."
        )
        self._llm_latency = meter.create_histogram(
            "foundry.llm.latency_ms", unit="ms", description="LLM call latency."
        )
        self._llm_cost = meter.create_counter(
            "foundry.llm.cost_usd", unit="usd", description="LLM call cost estimate."
        )
        self._tool_calls = meter.create_counter(
            "foundry.tool.calls_total", description="Tool dispatches by ref/version/success."
        )
        self._tool_latency = meter.create_histogram(
            "foundry.tool.latency_ms", unit="ms", description="Tool dispatch latency."
        )
        self._handoffs = meter.create_counter(
            "foundry.handoff_total", description="Flow handoffs by from/to/trigger."
        )
        self._eval_runs = meter.create_counter(
            "foundry.eval.runs_total", description="Eval harness invocations."
        )
        self._eval_score = meter.create_gauge(
            "foundry.eval.score", description="Latest eval score per project/target."
        )
        self._cache_saved_cost = meter.create_counter(
            "foundry.cache.semantic.saved_cost_usd",
            unit="usd",
            description="Cost avoided by semantic-cache hits.",
        )
        self._embed_cost = meter.create_counter(
            "foundry.embed.cost_usd", unit="usd", description="Embedding call cost estimate."
        )
        self._rollbacks = meter.create_counter(
            "foundry.rollback.total", description="Rollbacks by project/granularity."
        )
        self._deployments = meter.create_counter(
            "foundry.deployment.total", description="foundry deploy invocations by status."
        )

    # -- event-driven recordings -----------------------------------------

    def handle(self, event: BaseModel, *, project: str, provider: str, model: str) -> None:
        """Record the metrics an event implies. ``project`` / ``provider`` /
        ``model`` come from the per-run tracker in
        :mod:`foundry.observability.events` (llm.completed and run-terminal
        events don't carry them)."""
        if isinstance(event, LLMCallCompleted):
            dims = {"provider": provider, "model": model}
            self._llm_calls.add(1, {**dims, "agent": event.agent_name})
            self._llm_latency.record(event.latency_ms, dims)
            self._llm_cost.add(_cost(event.cost_estimate_usd), {**dims, "project": project})
            token_dims = {"project": project, **dims}
            self._run_input_tokens.add(event.usage.input_tokens, token_dims)
            self._run_output_tokens.add(event.usage.output_tokens, token_dims)
            if event.usage.reasoning_tokens:
                self._run_reasoning_tokens.add(event.usage.reasoning_tokens, token_dims)
        elif isinstance(event, ToolCompleted):
            self._tool_calls.add(
                1,
                {
                    "tool_ref": event.tool_ref,
                    "tool_version": event.tool_version,
                    "success": str(event.success).lower(),
                },
            )
            self._tool_latency.record(
                event.latency_ms,
                {"tool_ref": event.tool_ref, "tool_version": event.tool_version},
            )
        elif isinstance(event, Handoff):
            self._handoffs.add(
                1,
                {
                    "from_agent": event.from_agent,
                    "to_agent": event.to_agent,
                    "trigger": event.trigger,
                },
            )
        elif isinstance(event, RunCompleted):
            dims = {"project": project, "worker_id": event.worker_id}
            self._run_total.add(1, {**dims, "status": event.status})
            self._run_duration.record(event.duration_ms, dims)
            self._run_cost.add(_cost(event.total_cost_estimate_usd), dims)
        elif isinstance(event, RunFailed):
            self._run_total.add(
                1, {"project": project, "worker_id": event.worker_id, "status": "failed"}
            )
        elif isinstance(event, RunCancelledEvent):
            self._run_total.add(
                1, {"project": project, "worker_id": event.worker_id, "status": "cancelled"}
            )
        elif isinstance(event, SemanticCacheHitEvent):
            self._cache_saved_cost.add(
                _cost(event.saved_cost_estimate_usd),
                {"agent": event.agent_name, "project": project},
            )
        elif isinstance(event, EmbedCall):
            self._embed_cost.add(
                _cost(event.cost_estimate_usd), {"embedder": event.embedder}
            )

    # -- direct recordings (non-RunEvent operations) -----------------------

    def record_eval(
        self,
        *,
        project: str,
        target_ref: str,
        eval_spec_hash: str,
        score: float,
    ) -> None:
        self._eval_runs.add(1, {"project": project, "target_ref": target_ref})
        self._eval_score.set(
            score,
            {
                "project": project,
                "target_ref": target_ref,
                "eval_spec_hash": eval_spec_hash,
            },
        )

    def record_rollback(self, *, project: str, granularity: str) -> None:
        self._rollbacks.add(1, {"project": project, "granularity": granularity})

    def record_deployment(self, *, project: str, status: str) -> None:
        self._deployments.add(1, {"project": project, "status": status})


_recorder: MetricsRecorder | None = None


def get_metrics_recorder() -> MetricsRecorder:
    """Process singleton. Instruments bind to the MeterProvider that is
    global at first use; tests installing an SDK MeterProvider must do so
    before the first recording (the conftest fixture does)."""
    global _recorder
    if _recorder is None:
        _recorder = MetricsRecorder()
    return _recorder


def reset_metrics_recorder() -> None:
    """Testing hook: drop the singleton so instruments re-resolve against a
    freshly-installed MeterProvider."""
    global _recorder
    _recorder = None


__all__ = [
    "MetricsRecorder",
    "get_metrics_recorder",
    "reset_metrics_recorder",
]
