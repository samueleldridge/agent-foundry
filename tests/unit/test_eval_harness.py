"""Harness unit coverage (docs/40 § Runner) against a local tmp-path tool:
weighted scoring math, threshold semantics, timeouts, error isolation,
skips, artifact round-trip, load-time case validation."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from foundry.config import FoundryRoots
from foundry.core.errors import CompileError, ConfigLoadError, ConfigValidationError
from foundry.eval import load_eval_result, load_tool_target, run_eval
from foundry.eval.schemas import EvalSpec

TOOL_YAML = """\
name: echo
version: v1
description: Upper-cases text; can sleep or fail on demand (test fixture).
input_schema: schemas.py::EchoIn
output_schema: schemas.py::EchoOut
handler: handler.py::handle
timeout_s: 30.0
schema_version: 1
"""

SCHEMAS_PY = """\
from pydantic import BaseModel, ConfigDict


class EchoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    sleep_s: float = 0.0
    fail: bool = False


class EchoOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    length: int
"""

HANDLER_PY = """\
import asyncio

from schemas import EchoIn, EchoOut


async def handle(inputs: EchoIn, ctx) -> EchoOut:
    if inputs.sleep_s:
        await asyncio.sleep(inputs.sleep_s)
    if inputs.fail:
        raise ValueError("boom")
    return EchoOut(text=inputs.text.upper(), length=len(inputs.text))
"""


@pytest.fixture(autouse=True)
def _foundry_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "fh"))


@pytest.fixture
def roots(tmp_path: Path) -> FoundryRoots:
    tool_dir = tmp_path / "proj" / "tools" / "echo" / "v1"
    tool_dir.mkdir(parents=True)
    (tool_dir / "tool.yaml").write_text(TOOL_YAML)
    (tool_dir / "schemas.py").write_text(SCHEMAS_PY)
    (tool_dir / "handler.py").write_text(HANDLER_PY)
    (tool_dir / "eval.yaml").write_text("# standalone eval placeholder\n")
    (tool_dir / "README.md").write_text("# echo\n")
    return FoundryRoots(
        catalog_roots=[tmp_path / "catalog"],
        projects_root=tmp_path,
        project_name="proj",
    )


def _spec(cases: list[dict[str, Any]], scorers: list[dict[str, Any]],
          **overrides: Any) -> EvalSpec:
    payload: dict[str, Any] = {
        "name": "echo_eval",
        "scope": "tool",
        "target": "local/echo@v1",
        "cases": cases,
        "scorers": scorers,
        "threshold": 0.9,
        "schema_version": 1,
    }
    payload.update(overrides)
    return EvalSpec.model_validate(payload)


_EXACT_TEXT = {"kind": "exact", "name": "text_match",
               "config": {"field": "text"}}


@pytest.mark.unit
async def test_weighted_aggregation_math(roots: FoundryRoots) -> None:
    """docs/40 unit test 4: case weights x weighted scorer scores."""
    spec = _spec(
        cases=[
            # both scorers pass
            {"id": "both", "input": {"text": "hi"},
             "expected": {"text": "HI", "length": 2}, "weight": 1.0},
            # text passes, length fails -> case score 0.6
            {"id": "half", "input": {"text": "hey"},
             "expected": {"text": "HEY", "length": 99}, "weight": 3.0},
        ],
        scorers=[
            {"kind": "exact", "name": "text_match",
             "config": {"field": "text"}, "weight": 0.6},
            {"kind": "numeric", "name": "length_check", "weight": 0.4,
             "config": {"field": "length", "op": "eq",
                        "target_field": "expected.length"}},
        ],
    )
    result = await run_eval(spec, load_tool_target("local/echo", roots, version="v1"))
    by_id = {c.case_id: c for c in result.per_case}
    assert by_id["both"].score == pytest.approx(1.0)
    assert by_id["half"].score == pytest.approx(0.6)
    # aggregate: (1*1.0 + 3*0.6) / 4 = 0.7
    assert result.score == pytest.approx(0.7)
    assert result.passed is False  # threshold 0.9
    assert result.cases_passed == 1 and result.cases_failed == 1
    # per-scorer rollups
    assert result.per_scorer["text_match"].average_score == pytest.approx(1.0)
    assert result.per_scorer["length_check"].average_score == pytest.approx(0.5)
    assert result.per_scorer["length_check"].pass_rate == pytest.approx(0.5)


@pytest.mark.unit
async def test_threshold_boundary_is_inclusive(roots: FoundryRoots) -> None:
    """docs/40 unit test 5: score >= threshold passes; < fails."""
    spec = _spec(
        cases=[{"id": "ok", "input": {"text": "a"},
                "expected": {"text": "A"}}],
        scorers=[_EXACT_TEXT],
        threshold=1.0,
    )
    result = await run_eval(spec, load_tool_target("local/echo", roots, version="v1"))
    assert result.score == 1.0 and result.passed


@pytest.mark.unit
async def test_case_timeout_errors_case_and_run_continues(
    roots: FoundryRoots,
) -> None:
    """docs/40 unit test 6: a case sleeping past case_timeout_s errors with
    score 0.0; other cases still run."""
    spec = _spec(
        cases=[
            {"id": "slow", "input": {"text": "a", "sleep_s": 5.0},
             "expected": {"text": "A"}},
            {"id": "fast", "input": {"text": "b"}, "expected": {"text": "B"}},
        ],
        scorers=[_EXACT_TEXT],
        case_timeout_s=0.2,
        max_parallel=1,
    )
    result = await run_eval(spec, load_tool_target("local/echo", roots, version="v1"))
    by_id = {c.case_id: c for c in result.per_case}
    assert by_id["slow"].status == "error"
    assert by_id["slow"].score == 0.0
    assert by_id["slow"].error is not None
    assert "case_timeout_s" in by_id["slow"].error["message"]
    assert by_id["fast"].status == "scored" and by_id["fast"].score == 1.0
    assert result.score == pytest.approx(0.5)


@pytest.mark.unit
async def test_target_error_becomes_case_error(roots: FoundryRoots) -> None:
    spec = _spec(
        cases=[{"id": "boom", "input": {"text": "a", "fail": True},
                "expected": {"text": "A"}}],
        scorers=[_EXACT_TEXT],
    )
    result = await run_eval(spec, load_tool_target("local/echo", roots, version="v1"))
    case = result.per_case[0]
    assert case.status == "error" and case.score == 0.0
    assert case.error is not None
    assert case.error["error_class"] == "ToolHandlerError"
    assert result.cases_failed == 1


@pytest.mark.unit
async def test_skip_cases_are_excluded_from_the_aggregate(
    roots: FoundryRoots,
) -> None:
    spec = _spec(
        cases=[
            {"id": "run", "input": {"text": "a"}, "expected": {"text": "A"}},
            {"id": "skipped", "input": {"text": "b"},
             "expected": {"text": "WRONG"}, "skip": True,
             "skip_reason": "debugging"},
        ],
        scorers=[_EXACT_TEXT],
    )
    result = await run_eval(spec, load_tool_target("local/echo", roots, version="v1"))
    assert result.cases_skipped == 1
    assert result.score == 1.0  # the skipped (failing) case doesn't count
    skipped = next(c for c in result.per_case if c.case_id == "skipped")
    assert skipped.status == "skipped" and skipped.skip_reason == "debugging"


@pytest.mark.unit
async def test_scorer_failure_scores_zero_and_run_continues(
    roots: FoundryRoots,
) -> None:
    """docs/40 failure mode: scorer raises -> that scorer records 0.0; the
    other scorer still counts. An invalid regex pattern triggers it."""
    spec = _spec(
        cases=[{"id": "c", "input": {"text": "a"},
                "expected": {"text": "("}}],  # invalid regex -> re.error
        scorers=[
            {"kind": "exact", "name": "broken_regex", "weight": 0.5,
             "config": {"field": "text", "fuzzy": {"kind": "regex"}}},
            {"kind": "numeric", "name": "length_ok", "weight": 0.5,
             "config": {"field": "length", "op": "eq", "target_value": 1}},
        ],
    )
    events: list[Any] = []
    result = await run_eval(
        spec,
        load_tool_target("local/echo", roots, version="v1"),
        event_sink=events.append,
    )
    case = result.per_case[0]
    assert case.status == "scored"
    broken = next(s for s in case.scorer_results if s.scorer_name == "broken_regex")
    assert broken.score == 0.0 and broken.error is not None
    ok = next(s for s in case.scorer_results if s.scorer_name == "length_ok")
    assert ok.score == 1.0
    assert case.score == pytest.approx(0.5)
    warning_categories = [
        getattr(e, "category", None) for e in events
    ]
    assert "eval.scorer.error" in warning_categories


@pytest.mark.unit
async def test_replicates_run_in_non_deterministic_mode(
    roots: FoundryRoots,
) -> None:
    spec = _spec(
        cases=[{"id": "c", "input": {"text": "a"}, "expected": {"text": "A"}}],
        scorers=[_EXACT_TEXT],
        deterministic=False,
        replicates=3,
    )
    result = await run_eval(spec, load_tool_target("local/echo", roots, version="v1"))
    case = result.per_case[0]
    assert case.metadata["replicate_scores"] == [1.0, 1.0, 1.0]
    assert case.score == 1.0


@pytest.mark.unit
async def test_artifact_written_and_readable(
    roots: FoundryRoots, tmp_path: Path
) -> None:
    """docs/03 exit gate 5: artifact under ~/.foundry/runs/<eval_run_id>/,
    readable back via the foundry.eval utilities (Phase 6's surface)."""
    spec = _spec(
        cases=[{"id": "weird/id with spaces", "input": {"text": "a"},
                "expected": {"text": "A"}}],
        scorers=[_EXACT_TEXT],
    )
    result = await run_eval(spec, load_tool_target("local/echo", roots, version="v1"))
    directory = Path(result.metadata["artifact_dir"])
    assert directory == tmp_path / "fh" / "runs" / str(result.eval_run_id)
    assert (directory / "eval_result.json").exists()
    case_files = list((directory / "cases").glob("*.json"))
    assert len(case_files) == 1  # sanitized filename
    json.loads(case_files[0].read_text())

    by_id = load_eval_result(str(result.eval_run_id))
    assert by_id == result
    by_path = load_eval_result(directory)
    assert by_path.eval_run_id == result.eval_run_id


@pytest.mark.unit
async def test_case_input_validated_at_load_naming_the_case(
    roots: FoundryRoots,
) -> None:
    """docs/40 unit test 2 + invariant 2: bad case input -> load-time
    ConfigValidationError carrying the case id; nothing ran."""
    spec = _spec(
        cases=[{"id": "bad_shape", "input": {"txt": "typo"},
                "expected": {"text": "X"}}],
        scorers=[_EXACT_TEXT],
    )
    with pytest.raises(ConfigValidationError, match="bad_shape"):
        await run_eval(spec, load_tool_target("local/echo", roots, version="v1"))


@pytest.mark.unit
async def test_scope_mismatch_is_rejected(roots: FoundryRoots) -> None:
    spec = _spec(
        cases=[{"id": "c", "input": {"text": "a"}, "expected": {}}],
        scorers=[_EXACT_TEXT],
    ).model_copy(update={"scope": "project"})
    with pytest.raises(ConfigValidationError, match="scope"):
        await run_eval(spec, load_tool_target("local/echo", roots, version="v1"))


@pytest.mark.unit
def test_tool_with_required_connections_is_refused_standalone(
    tmp_path: Path,
) -> None:
    """Phase 4 limitation, surfaced as a structured compile error rather
    than a mid-eval crash."""
    tool_dir = tmp_path / "proj" / "tools" / "needs_conn" / "v1"
    tool_dir.mkdir(parents=True)
    (tool_dir / "tool.yaml").write_text(
        "name: needs_conn\nversion: v1\ndescription: d\n"
        "input_schema: schemas.py::In\noutput_schema: schemas.py::Out\n"
        "handler: handler.py::handle\n"
        "connections_required:\n"
        "  - slot: service\n    accepts: [catalog/http_service]\n"
        "schema_version: 1\n"
    )
    (tool_dir / "schemas.py").write_text(
        "from pydantic import BaseModel\n\n"
        "class In(BaseModel):\n    pass\n\n"
        "class Out(BaseModel):\n    pass\n"
    )
    (tool_dir / "handler.py").write_text(
        "async def handle(inputs, ctx):\n    return {}\n"
    )
    (tool_dir / "eval.yaml").write_text("# placeholder\n")
    (tool_dir / "README.md").write_text("# needs_conn\n")
    roots = FoundryRoots(
        catalog_roots=[tmp_path / "catalog"],
        projects_root=tmp_path,
        project_name="proj",
    )
    with pytest.raises(CompileError, match="connection slot"):
        load_tool_target("local/needs_conn", roots, version="v1")


@pytest.mark.unit
def test_load_eval_result_missing_is_structured(tmp_path: Path) -> None:
    with pytest.raises(ConfigLoadError, match="no eval result"):
        load_eval_result("01BX5ZZKBKACTAV9WEVGEMMVRZ")
