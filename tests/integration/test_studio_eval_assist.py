"""AI-assisted eval authoring routes (docs/72 § Eval assistant).

Scripted httpx.MockTransport speaking the openai wire shape (the
assistant default binding is openai/gpt-5-mini): the handler answers the
questions call, the draft call, and — when scripted — the one automatic
repair round-trip. No route here may write a byte into the project tree;
saving is the human's act through the config-write route.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest
from starlette.testclient import TestClient
from studio_helpers import git, make_studio_repo

from foundry.studio.app import create_studio_app

pytestmark = pytest.mark.integration

QUESTIONS_PAYLOAD = {
    "questions": [
        {
            "id": "input_shape",
            "question": "What fields does the agent's input carry?",
            "why": "Cases must match the input schema.",
            "suggested_answer": '{"name": "world"}',
        },
        {
            "id": "output_shape",
            "question": "What does a correct output look like?",
            "why": "Expected values are the scoring target.",
            "suggested_answer": '{"greeting": "Hello, world!"}',
        },
        {
            "id": "edge_cases",
            "question": "Which edge cases matter (empty name, unicode)?",
            "why": "Edge cases keep the eval honest.",
            "suggested_answer": None,
        },
    ]
}

VALID_DRAFT_YAML = """\
name: hello_assist_draft
description: The greeting must address the caller by name.
scope: project
target: hello
cases:
  - id: plain_name
    input: { name: "world" }
    expected: { greeting: "Hello, world!" }
    tags: [smoke]
  - id: unicode_name
    input: { name: "Zoë" }
    expected: { greeting: "Hello, Zoë!" }
    tags: [edge]
scorers:
  - kind: exact
    name: greeting_match
    config: { field: greeting }
threshold: 0.9
deterministic: true
seed: 42
schema_version: 1
"""

# Scorer weights sum to 0.5 → the REAL EvalSpec validator rejects it.
INVALID_DRAFT_YAML = VALID_DRAFT_YAML.replace(
    "    config: { field: greeting }",
    "    config: { field: greeting }\n    weight: 0.5",
)

# llm_judge in a machine-generated draft → assistant guardrail error.
JUDGED_DRAFT_YAML = VALID_DRAFT_YAML.replace(
    """scorers:
  - kind: exact
    name: greeting_match
    config: { field: greeting }""",
    """scorers:
  - kind: llm_judge
    name: greeting_judge
    config:
      judge_model_binding: { provider: openai, model: gpt-5-mini }
      rubric_template: "Compare {expected} with {actual}."
""",
)


class ScriptedAssistTransport(httpx.AsyncBaseTransport):
    """Answers openai chat-completions calls from a per-role script:
    'questions' requests get the questions payload; 'draft' requests pop
    the next draft body (so a repair round-trip serves the second one)."""

    def __init__(self, drafts: list[dict[str, Any]]) -> None:
        self.drafts = list(drafts)
        self.requests: list[dict[str, Any]] = []

    async def handle_async_request(
        self, request: httpx.Request
    ) -> httpx.Response:
        body = json.loads(request.content.decode())
        self.requests.append(body)
        system_text = body["messages"][0]["content"]
        if "clarifying questions" in system_text:
            payload = QUESTIONS_PAYLOAD
        else:
            payload = self.drafts.pop(0)
        return httpx.Response(
            200,
            json={
                "model": body.get("model", "gpt-5-mini"),
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(payload),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 120, "completion_tokens": 80},
            },
        )


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    repo = make_studio_repo(tmp_path)
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(repo / "catalog"))
    return repo


def _client(
    repo: Path, transport: httpx.AsyncBaseTransport | None
) -> TestClient:
    return TestClient(
        create_studio_app(repo, transport=transport, serve_assets=False)
    )


def _tree_state(repo: Path) -> tuple[str, str]:
    head = git(repo, "rev-parse", "HEAD").strip()
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return head, status


def test_questions_roundtrip_and_cost_lands_in_mirror(repo: Path) -> None:
    """POST /questions → 3-5 structured questions; the LLM call mirrors
    into the observability store with project attribution (cost rows)."""
    from foundry.observability.events import get_store

    transport = ScriptedAssistTransport(drafts=[])
    with _client(repo, transport) as client:
        response = client.post(
            "/api/evals/assist/questions",
            json={
                "project": "hello",
                "description": "Greet the caller by name.",
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["project"] == "hello"
    assert body["model"] == "openai/gpt-5-mini"
    assert 3 <= len(body["questions"]) <= 5
    assert body["questions"][0]["id"] == "input_shape"
    assert body["questions"][0]["suggested_answer"] is not None

    # Charge/observe: the call landed as an llm_calls row for the project.
    rows = get_store().cost_breakdown(project="hello", by="model")
    assert rows and rows[0]["bucket"] == "openai:gpt-5-mini"
    assert rows[0]["calls"] == 1
    assert rows[0]["cost_usd"] > 0
    # ...and the control-plane act is in the studio_events audit table.
    kinds = {
        row["event"] for row in get_store().studio_events(project="hello")
    }
    assert "studio.eval_assist_questions" in kinds


def test_draft_roundtrip_valid_and_never_touches_disk(repo: Path) -> None:
    """POST /draft returns validated YAML + parsed case rows; NOTHING is
    written or committed — saving is the human's explicit act."""
    transport = ScriptedAssistTransport(
        drafts=[{"yaml": VALID_DRAFT_YAML, "notes": ["check unicode case"]}]
    )
    before = _tree_state(repo)
    evals_dir = repo / "projects" / "hello" / "evals"
    files_before = sorted(p.name for p in evals_dir.iterdir())
    with _client(repo, transport) as client:
        response = client.post(
            "/api/evals/assist/draft",
            json={
                "project": "hello",
                "description": "Greet the caller by name.",
                "answers": [
                    {"id": "input_shape", "answer": '{"name": "world"}'},
                    {"id": "output_shape", "answer": "greeting string"},
                ],
                "case_count": 2,
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["validation"]["ok"] is True
    assert body["yaml"] == VALID_DRAFT_YAML
    assert body["suggested_path"] == "evals/hello.yaml"
    assert "check unicode case" in body["notes"]
    case_ids = [case["id"] for case in body["cases"]]
    assert case_ids == ["plain_name", "unicode_name"]
    # jump-to-line targets point at the case's `id:` line
    assert body["cases"][0]["line"] is not None
    assert (
        "plain_name"
        in VALID_DRAFT_YAML.splitlines()[body["cases"][0]["line"] - 1]
    )
    assert body["cases"][0]["input"] == {"name": "world"}
    assert body["cases"][0]["expected"] == {"greeting": "Hello, world!"}

    # THE guarantee: the draft never reached the tree (no new file, no
    # modification, no commit).
    assert sorted(p.name for p in evals_dir.iterdir()) == files_before
    assert _tree_state(repo) == before
    # And only one draft LLM call happened (no hidden repair round).
    draft_calls = [
        req
        for req in transport.requests
        if "clarifying questions" not in req["messages"][0]["content"]
    ]
    assert len(draft_calls) == 1


def test_invalid_draft_triggers_one_repair_roundtrip(repo: Path) -> None:
    """First draft fails the REAL EvalSpec loader (weights sum 0.5) → the
    route feeds the issues back to the LLM exactly once; the repaired
    draft validates."""
    transport = ScriptedAssistTransport(
        drafts=[
            {"yaml": INVALID_DRAFT_YAML, "notes": []},
            {"yaml": VALID_DRAFT_YAML, "notes": ["weights fixed"]},
        ]
    )
    with _client(repo, transport) as client:
        response = client.post(
            "/api/evals/assist/draft",
            json={
                "project": "hello",
                "description": "Greet the caller by name.",
                "answers": [],
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["validation"]["ok"] is True
    assert body["yaml"] == VALID_DRAFT_YAML
    assert any("repaired" in note for note in body["notes"])

    draft_calls = [
        req
        for req in transport.requests
        if "clarifying questions" not in req["messages"][0]["content"]
    ]
    assert len(draft_calls) == 2
    # The repair prompt carried the loader's own issue text + prior draft.
    repair_user = draft_calls[1]["messages"][1]["content"]
    assert "failed validation" in repair_user
    assert "weights" in repair_user
    assert "hello_assist_draft" in repair_user


def test_repair_exhausted_returns_issues_not_500(repo: Path) -> None:
    """Both drafts invalid → 200 with validation.ok false + issues (the
    human fixes the rest in the review editor); still nothing on disk."""
    transport = ScriptedAssistTransport(
        drafts=[
            {"yaml": INVALID_DRAFT_YAML, "notes": []},
            {"yaml": INVALID_DRAFT_YAML, "notes": []},
        ]
    )
    before = _tree_state(repo)
    with _client(repo, transport) as client:
        response = client.post(
            "/api/evals/assist/draft",
            json={
                "project": "hello",
                "description": "Greet the caller by name.",
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["validation"]["ok"] is False
    assert body["validation"]["issues"]
    assert any("repair" in note for note in body["notes"])
    assert _tree_state(repo) == before


def test_llm_judge_in_generated_draft_is_refused(repo: Path) -> None:
    """The assistant guardrail: a generated draft carrying llm_judge is a
    validation error (twice → surfaced to the human), even though the
    schema itself would accept the scorer."""
    transport = ScriptedAssistTransport(
        drafts=[
            {"yaml": JUDGED_DRAFT_YAML, "notes": []},
            {"yaml": JUDGED_DRAFT_YAML, "notes": []},
        ]
    )
    with _client(repo, transport) as client:
        response = client.post(
            "/api/evals/assist/draft",
            json={
                "project": "hello",
                "description": "Greet the caller by name.",
            },
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["validation"]["ok"] is False
    assert any(
        "llm_judge" in issue["message"]
        for issue in body["validation"]["issues"]
    )


def test_missing_provider_key_is_424(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No key for the chosen provider → 424 unavailable with the env var
    + remedy in the envelope (same contract as unavailable projects)."""
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with _client(repo, ScriptedAssistTransport(drafts=[])) as client:
        response = client.post(
            "/api/evals/assist/questions",
            json={
                "project": "hello",
                "description": "Greet the caller by name.",
            },
        )
    assert response.status_code == 424, response.text
    body = response.json()
    assert body["context"]["env_vars"] == ["OPENAI_API_KEY"]
    assert "Providers" in body["context"]["remedy"]


def test_unknown_project_is_404_before_any_llm_call(repo: Path) -> None:
    transport = ScriptedAssistTransport(drafts=[])
    with _client(repo, transport) as client:
        response = client.post(
            "/api/evals/assist/questions",
            json={"project": "nope", "description": "anything"},
        )
    assert response.status_code == 404
    assert transport.requests == []


def test_malformed_llm_payload_is_structured_502(repo: Path) -> None:
    """A model answering outside the structured shape surfaces as a
    ProviderError envelope, never a stack trace."""

    class GarbageTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(
            self, request: httpx.Request
        ) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "model": "gpt-5-mini",
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "sorry, I prefer prose",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                    },
                },
            )

    with _client(repo, GarbageTransport()) as client:
        response = client.post(
            "/api/evals/assist/questions",
            json={
                "project": "hello",
                "description": "Greet the caller by name.",
            },
        )
    assert response.status_code == 502, response.text
    assert response.json()["error_class"] == "ProviderError"
    assert "malformed" in response.json()["message"]
