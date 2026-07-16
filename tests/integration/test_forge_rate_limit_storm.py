"""Forge resilience under provider rate limits (429 storms).

Operator-reported failure mode: TPM 429s used to kill a forge run with
``provider_failure`` after two adapter retries. With the rate-limit
backoff schedule (docs/11 § Retry policy) a 429 storm degrades the run to
SLOWER, not failed: the adapter honours Retry-After hints, backs off with
full jitter, and surfaces every wait as a ``provider.retry`` event on the
forge trajectory stream.

Same harness as the other forge suites: scripted meta turns + computed
project turns behind ONE MockTransport, wrapped here in a storm that 429s
the first attempts per model (with OpenAI-style body hints and
Retry-After headers) before letting the real response through.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from forge_helpers import (
    META_MODEL,
    PROMPT_WITH_BOTH,
    PROMPT_WITH_DIGIT,
    ForgeTransport,
    make_repo,
    prompt_iteration_turns,
    write_scaffolded_project,
)
from pydantic import BaseModel

from foundry.configurator import ForgeGuardrails, ForgeSession, MetaAgent

pytestmark = pytest.mark.integration


class StormTransport:
    """429s the first ``per_model`` requests of each model — alternating
    the Retry-After header and the OpenAI-style body hint — then delegates
    to the scripted ForgeTransport."""

    def __init__(self, inner: ForgeTransport, per_model: int = 2) -> None:
        self.inner = inner
        self.per_model = per_model
        self.counts: dict[str, int] = {}
        self.storm_hits = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model = str(body["model"])
        seen = self.counts.get(model, 0)
        if seen < self.per_model:
            self.counts[model] = seen + 1
            self.storm_hits += 1
            if seen % 2 == 0:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "0.01"},
                    json={"error": {"message": "TPM limit exceeded"}},
                )
            return httpx.Response(
                429,
                json={
                    "error": {
                        "message": (
                            "Rate limit reached. Please try again in 12ms."
                        )
                    }
                },
            )
        return self.inner.handler(request)

    def build(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    # Keep any jittered (hint-less) backoff test-fast; attempts default (8).
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_MAX_BACKOFF_S", "0.05")


async def test_forge_survives_a_429_storm_and_reaches_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = make_repo(tmp_path)
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(repo / "catalog"))
    # Baseline 0.833 (reverse rule missing); one prompt iteration -> 1.0.
    write_scaffolded_project(repo, prompt_v1=PROMPT_WITH_DIGIT)

    storm = StormTransport(
        ForgeTransport(
            prompt_iteration_turns(
                new_version="v2",
                content=PROMPT_WITH_BOTH,
                cluster_id="reverse_questions_scorer_answer_match",
                summary="prompt v1 -> v2: explicit reverse rule",
                eval_before=0.833,
            )
        ),
        per_model=2,
    )
    events: list[BaseModel] = []
    agent = MetaAgent(
        "qa_bot",
        projects_root=repo / "projects",
        guardrails=ForgeGuardrails(max_iter=3),
        transport=storm.build(),
    )
    session = ForgeSession(
        meta_agent=agent,
        description="Numeric-answer QA over three question kinds.",
        eval_spec_path=Path("projects/qa_bot/evals/qa.yaml"),
        threshold=0.9,
        event_sink=events.append,
    )
    result = await session.run()

    # The storm actually happened...
    assert storm.storm_hits >= 4, storm.counts
    # ...and the run completed instead of dying provider_failure.
    assert result.termination_reason == "threshold_met", (
        result.termination_reason,
        result.termination_detail,
    )
    assert result.threshold_met
    assert result.final_score == 1.0

    # The meta-agent's backoffs surfaced on the forge event stream with
    # the computed delay ("backing off Ns (rate limited)").
    retries = [
        event
        for event in events
        if getattr(event, "event", "") == "provider.retry"
    ]
    assert retries, "provider.retry events must reach the forge sink"
    for retry in retries:
        payload = retry.model_dump()
        assert payload["rate_limited"] is True
        assert payload["delay_s"] >= 0.0
        assert payload["error_class"] == "ProviderRateLimitError"
        assert payload["agent_name"] == "meta_agent"
    meta_hits = storm.counts.get(META_MODEL, 0)
    assert len(retries) == meta_hits == 2

    # And the trajectory artifact recorded them (events.jsonl).
    events_file = Path(result.artifact_dir) / "events.jsonl"
    recorded = [
        json.loads(line)
        for line in events_file.read_text().splitlines()
        if line.strip()
    ]
    recorded_retries: list[dict[str, Any]] = [
        row for row in recorded if row.get("event") == "provider.retry"
    ]
    assert recorded_retries
    assert all("delay_s" in row for row in recorded_retries)
