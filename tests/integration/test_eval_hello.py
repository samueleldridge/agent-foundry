"""Phase 4 exit-gate integration tests: project / agent / judged evals
against projects/hello (and memory_hello for the memory turn loop), all on
httpx.MockTransport fakes — the established no-live-keys pattern.

Gates covered here (docs/03 § Phase 4):
- 5-case hello project eval runs -> result with score + per-case details.
- llm_judge goes through the provider abstraction (anthropic agent judged
  by an openai-bound judge through ONE transport).
- eval artifact lands under ~/.foundry/runs/<eval_run_id>/ and reads back.
- determinism: same eval + seed -> same score.
- --fail-under 0.9 exits non-zero below the floor.
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from foundry.cli.eval import execute_eval
from foundry.eval import list_eval_history, load_eval_result

REPO_ROOT = Path(__file__).resolve().parents[2]
HELLO_DIR = REPO_ROOT / "projects" / "hello"
MEMORY_DIR = REPO_ROOT / "projects" / "memory_hello"


@pytest.fixture(autouse=True)
def _isolated_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[None]:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "fake-anthropic-key-for-tests")
    monkeypatch.setenv("OPENAI_API_KEY", "fake-openai-key-for-tests")
    monkeypatch.setenv("HELLO_SERVICE_API_KEY", "fake-service-key-for-tests")
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))
    yield
    # eval history is appended inside the repo's project trees (gitignored);
    # keep the working tree clean between tests
    for project in (HELLO_DIR, MEMORY_DIR):
        state = project / ".foundry"
        if state.exists():
            shutil.rmtree(state)


def _anthropic_reply(payload: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "content": [{"type": "text", "text": json.dumps(payload)}],
            "stop_reason": "end_turn",
            "model": "claude-haiku-4-5",
            "usage": {"input_tokens": 50, "output_tokens": 20},
        },
    )


def _greeter_transport(*, name_in_greeting: bool = True) -> httpx.MockTransport:
    """Anthropic fake that reads the caller's name out of the request and
    greets (or pointedly fails to greet) by name."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.anthropic.com"
        body = json.loads(request.content)
        user_text = body["messages"][0]["content"][0]["text"]
        name = json.loads(user_text)["name"]
        greeting = (
            f"Hello, {name}! Lovely to meet you."
            if name_in_greeting
            else "Hello there, wonderful stranger!"
        )
        return _anthropic_reply({"greeting": greeting})

    return httpx.MockTransport(handler)


def _eval_runs_root(tmp_path: Path) -> Path:
    return tmp_path / "foundry_home" / "runs"


# --- exit gate 1: 5-case project eval ------------------------------------------------


@pytest.mark.integration
def test_hello_project_eval_five_cases_scores_and_details(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = execute_eval(
        str(HELLO_DIR), ["evals/greeting.yaml"], transport=_greeter_transport()
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "Score: 1.00" in out and "PASSED" in out
    assert "Cases: 5 (passed: 5, failed: 0, skipped: 0)" in out

    # exactly one eval artifact; readable via the Phase 6 surface
    run_dirs = list(_eval_runs_root(tmp_path).iterdir())
    assert len(run_dirs) == 1
    result = load_eval_result(run_dirs[0])
    assert result.eval_name == "hello_greeting"
    assert result.scope == "project"
    assert result.cases_total == 5
    assert result.score == 1.0 and result.passed
    assert len(result.per_case) == 5
    assert all(c.scorer_results for c in result.per_case)
    assert result.pin_set_hash  # project evals carry the pin-set hash
    assert result.tokens_total == 5 * 70
    # per-case detail files exist
    assert len(list((run_dirs[0] / "cases").glob("*.json"))) == 5
    # eval history appended in the project tree
    history = list_eval_history(HELLO_DIR)
    assert history[-1]["eval_run_id"] == str(result.eval_run_id)
    assert history[-1]["passed"] is True


# --- determinism gate ------------------------------------------------------------------


@pytest.mark.integration
def test_deterministic_eval_reproduces_the_score(tmp_path: Path) -> None:
    """Same system + eval set + seed -> same score (mock transport makes
    the model side exactly reproducible; the harness must not add noise).
    Deterministic mode also forces temperature 0 on the request."""
    seen_temperatures: list[Any] = []
    base = _greeter_transport()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen_temperatures.append(body.get("temperature"))
        return base.handler(request)  # type: ignore[attr-defined]

    scores: list[float] = []
    for _ in range(2):
        code = execute_eval(
            str(HELLO_DIR),
            ["evals/greeting.yaml"],
            transport=httpx.MockTransport(handler),
        )
        assert code == 0
    for run_dir in sorted(_eval_runs_root(tmp_path).iterdir()):
        scores.append(load_eval_result(run_dir).score)
    assert scores == [1.0, 1.0]
    assert all(t == 0.0 for t in seen_temperatures), seen_temperatures


# --- fail-under gate --------------------------------------------------------------------


@pytest.mark.integration
def test_fail_under_returns_nonzero_below_floor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = execute_eval(
        str(HELLO_DIR),
        ["evals/greeting.yaml"],
        fail_under=0.9,
        transport=_greeter_transport(name_in_greeting=False),
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "Top failures:" in out


@pytest.mark.integration
def test_fail_under_passes_at_or_above_floor(tmp_path: Path) -> None:
    code = execute_eval(
        str(HELLO_DIR),
        ["evals/greeting.yaml"],
        fail_under=0.9,
        transport=_greeter_transport(),
    )
    assert code == 0


# --- infrastructure failure = exit 2 ------------------------------------------------------


@pytest.mark.integration
def test_all_cases_erroring_is_infrastructure_exit_2(tmp_path: Path) -> None:
    """docs/40 § CI integration: exit 2 when the eval could not actually
    run (here: provider auth rejected on every case)."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "bad key"}})

    code = execute_eval(
        str(HELLO_DIR),
        ["evals/greeting.yaml"],
        transport=httpx.MockTransport(handler),
    )
    assert code == 2
    run_dirs = list(_eval_runs_root(tmp_path).iterdir())
    result = load_eval_result(run_dirs[0])
    assert all(c.status == "error" for c in result.per_case)
    assert all(
        c.error is not None and c.error["error_class"] == "ProviderAuthError"
        for c in result.per_case
    )


# --- exit gate 4: llm_judge through the provider abstraction ------------------------------


@pytest.mark.integration
def test_llm_judge_uses_provider_abstraction_cross_vendor(
    tmp_path: Path,
) -> None:
    """The agent under test is anthropic-bound; the judge ModelBinding is
    openai-bound. ONE transport serves both hosts — proving the judge call
    goes through the registered provider adapters, nothing hardcoded."""
    judge_calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.anthropic.com":
            body = json.loads(request.content)
            user_text = body["messages"][0]["content"][0]["text"]
            name = json.loads(user_text)["name"]
            return _anthropic_reply(
                {"greeting": f"Hello, {name}! Wonderful to see you."}
            )
        assert request.url.host == "api.openai.com"
        judge_calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "gpt-4o-mini",
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                {"score": 1.0,
                                 "rationale": "Named and warm."}
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 80, "completion_tokens": 15},
            },
        )

    code = execute_eval(
        str(HELLO_DIR),
        ["evals/greeting_judged.yaml"],
        transport=httpx.MockTransport(handler),
    )
    assert code == 0
    assert len(judge_calls) == 3  # one judge call per case
    # the rendered rubric reached the judge with the actual output woven in
    judge_prompt = json.dumps(judge_calls[0])
    assert "Actual output" in judge_prompt
    # judge is deterministic-mode too: temperature 0 + seed (openai supports it)
    assert judge_calls[0]["temperature"] == 0.0
    assert judge_calls[0]["seed"] == 42

    run_dirs = list(_eval_runs_root(tmp_path).iterdir())
    result = load_eval_result(run_dirs[0])
    assert result.score == 1.0
    judge_results = [
        s
        for c in result.per_case
        for s in c.scorer_results
        if s.scorer_name == "warm_greeting_judge"
    ]
    assert len(judge_results) == 3
    assert all(not s.is_deterministic for s in judge_results)
    assert all(s.metadata["judge_provider"] == "openai" for s in judge_results)
    assert all(s.metadata["rationale"] == "Named and warm." for s in judge_results)
    # judge tokens/cost fold into the case tallies
    assert all(c.tokens == 70 + 95 for c in result.per_case)


# --- agent scope (incl. the memory turn loop) ---------------------------------------------


@pytest.mark.integration
def test_agent_eval_runs_agent_in_isolation(tmp_path: Path) -> None:
    code = execute_eval(
        "agent",
        [str(HELLO_DIR), "hello_agent"],
        transport=_greeter_transport(),
    )
    assert code == 0
    run_dirs = list(_eval_runs_root(tmp_path).iterdir())
    result = load_eval_result(run_dirs[0])
    assert result.scope == "agent"
    assert result.target_ref == "hello_agent"
    assert result.target_version == "v2"  # the pinned prompt version
    assert result.cases_skipped == 1  # the documented skip case
    assert result.score == 1.0
    assert result.metadata["per_agent"] == {"hello_agent": 1.0}


@pytest.mark.integration
def test_agent_eval_unknown_agent_is_structured_error(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = execute_eval(
        "agent", [str(HELLO_DIR), "ghost_agent"], transport=_greeter_transport()
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "ghost_agent" in err and "hello_agent" in err


@pytest.mark.integration
def test_memory_agent_eval_drives_the_turn_loop(tmp_path: Path) -> None:
    """Agent-scope eval on a MEMORY agent: the harness's step driver walks
    turn -> llm -> turn_end for each turn; the final Reply is scored."""
    agent_turns = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal agent_turns
        assert request.url.host == "api.anthropic.com"
        body = json.loads(request.content)
        user_text = body["messages"][-1]["content"][0]["text"]
        if "user_facts consolidator" in user_text:
            return httpx.Response(
                200,
                json={
                    "content": [{"type": "text", "text": "- knows Sam"}],
                    "stop_reason": "end_turn",
                    "model": "claude-haiku-4-5",
                    "usage": {"input_tokens": 40, "output_tokens": 10},
                },
            )
        agent_turns += 1
        return _anthropic_reply({"reply": f"ack-{agent_turns}"})

    eval_dir = tmp_path / "memory_eval"
    eval_dir.mkdir()
    eval_path = eval_dir / "two_turns.yaml"
    eval_path.write_text(
        "name: memory_two_turns\n"
        "scope: agent\n"
        "target: hello_agent\n"
        "cases:\n"
        "  - id: two_turn_conversation\n"
        "    input:\n"
        "      turns: [\"hi there\", \"what did I say?\"]\n"
        "    expected: { reply: \"ack-2\" }\n"
        "scorers:\n"
        "  - kind: exact\n"
        "    name: final_reply\n"
        "    config: { field: reply }\n"
        "threshold: 1.0\n"
        "deterministic: true\n"
        "schema_version: 1\n"
    )
    code = execute_eval(
        "agent",
        [str(MEMORY_DIR), "hello_agent"],
        eval_option=str(eval_path),
        transport=httpx.MockTransport(handler),
    )
    assert code == 0
    assert agent_turns == 2  # one LLM turn per conversation turn
    run_dirs = list(_eval_runs_root(tmp_path).iterdir())
    result = load_eval_result(run_dirs[0])
    assert result.score == 1.0
    assert result.per_case[0].actual == {"reply": "ack-2"}


# --- show / list ---------------------------------------------------------------------------


@pytest.mark.integration
def test_show_and_list_read_back_persisted_results(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = execute_eval(
        str(HELLO_DIR), ["greeting"], transport=_greeter_transport()
    )
    assert code == 0
    result = load_eval_result(next(_eval_runs_root(tmp_path).iterdir()))
    capsys.readouterr()

    assert execute_eval("show", [str(result.eval_run_id)]) == 0
    out = capsys.readouterr().out
    assert "hello_greeting" in out and "Score: 1.00" in out

    assert execute_eval("list", [str(HELLO_DIR)]) == 0
    out = capsys.readouterr().out
    assert str(result.eval_run_id) in out and "PASS" in out


@pytest.mark.integration
def test_json_output_dumps_the_typed_result(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = execute_eval(
        str(HELLO_DIR),
        ["evals/greeting.yaml"],
        json_output=True,
        transport=_greeter_transport(),
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["eval_name"] == "hello_greeting"
    assert payload["cases_total"] == 5
    assert payload["per_case"][0]["pass"] is True


# --- cost caps -----------------------------------------------------------------------------


@pytest.mark.integration
def test_max_total_cost_halts_run_and_skips_remaining(
    tmp_path: Path,
) -> None:
    """docs/40 failure mode: max_total_cost_usd exceeded -> run halts,
    remaining cases skipped, partial result reported."""
    spec_path = tmp_path / "capped.yaml"
    spec_path.write_text(
        "name: capped_greeting\n"
        "scope: project\n"
        "target: hello\n"
        "cases:\n"
        "  - id: first\n"
        "    input: { name: \"world\" }\n"
        "    expected: { greeting: \"world\" }\n"
        "  - id: second\n"
        "    input: { name: \"Ada\" }\n"
        "    expected: { greeting: \"Ada\" }\n"
        "  - id: third\n"
        "    input: { name: \"Grace\" }\n"
        "    expected: { greeting: \"Grace\" }\n"
        "scorers:\n"
        "  - kind: exact\n"
        "    name: mentions_name\n"
        "    config: { field: greeting, fuzzy: { kind: regex } }\n"
        "threshold: 0.9\n"
        "max_parallel: 1\n"
        "deterministic: true\n"
        "max_total_cost_usd: \"0.0000001\"\n"
        "schema_version: 1\n"
    )
    code = execute_eval(
        str(HELLO_DIR), [str(spec_path)], transport=_greeter_transport()
    )
    result = load_eval_result(next(_eval_runs_root(tmp_path).iterdir()))
    assert result.cases_skipped == 2
    statuses = {c.case_id: c.status for c in result.per_case}
    assert statuses["first"] == "scored"
    assert statuses["second"] == statuses["third"] == "skipped"
    assert "max_total_cost_usd" in result.metadata["halted_reason"]
    skipped = next(c for c in result.per_case if c.case_id == "second")
    assert skipped.skip_reason is not None
    assert "max_total_cost_usd" in skipped.skip_reason
    # first case passed -> aggregate over the RUN cases is 1.0 -> exit 0
    assert code == 0


@pytest.mark.integration
def test_case_max_cost_budget_errors_the_case(tmp_path: Path) -> None:
    """docs/40 failure mode: case_max_cost_usd exceeded -> the case errors
    with CostBudgetExceeded (pre-call, so no HTTP happens)."""
    spec_path = tmp_path / "case_capped.yaml"
    spec_path.write_text(
        "name: case_capped_greeting\n"
        "scope: project\n"
        "target: hello\n"
        "cases:\n"
        "  - id: too_pricey\n"
        "    input: { name: \"world\" }\n"
        "    expected: { greeting: \"world\" }\n"
        "scorers:\n"
        "  - kind: exact\n"
        "    name: mentions_name\n"
        "    config: { field: greeting, fuzzy: { kind: regex } }\n"
        "deterministic: true\n"
        "case_max_cost_usd: \"0.00000001\"\n"
        "schema_version: 1\n"
    )
    http_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal http_calls
        http_calls += 1
        return httpx.Response(200, json={})

    code = execute_eval(
        str(HELLO_DIR), [str(spec_path)], transport=httpx.MockTransport(handler)
    )
    assert code == 2  # every (single) case errored -> infrastructure exit
    assert http_calls == 0, "budget must fire BEFORE any provider HTTP call"
    result = load_eval_result(next(_eval_runs_root(tmp_path).iterdir()))
    case = result.per_case[0]
    assert case.status == "error"
    assert case.error is not None
    assert case.error["error_class"] == "CostBudgetExceeded"
