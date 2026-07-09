"""Phase 4 exit-gate integration tests: tool-scope evals + cross-version
comparison against the catalog word_count v1/v2 pair (docs/03 gate 2)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foundry.cli.eval import execute_eval
from foundry.eval import load_eval_result

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_HOME", str(tmp_path / "foundry_home"))
    monkeypatch.setenv("FOUNDRY_CATALOG_ROOTS", str(REPO_ROOT / "catalog"))
    monkeypatch.chdir(REPO_ROOT)


def _runs_root(tmp_path: Path) -> Path:
    return tmp_path / "foundry_home" / "runs"


@pytest.mark.integration
def test_tool_eval_v1_passes_its_own_eval(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = execute_eval("tool", ["catalog/word_count@v1"])
    assert code == 0
    out = capsys.readouterr().out
    assert "word_count_v1_eval" in out and "PASSED" in out

    result = load_eval_result(next(_runs_root(tmp_path).iterdir()))
    assert result.scope == "tool"
    assert result.target_ref == "catalog/word_count@v1"
    assert result.target_version == "v1"
    assert result.pin_set_hash == ""  # tool scope carries no pin set
    assert result.score == 1.0
    assert result.tokens_total == 0  # no LLM anywhere in a pure tool eval


@pytest.mark.integration
def test_tool_eval_v2_passes_and_v1_fails_v2_contract(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert execute_eval("tool", ["catalog/word_count@v2"]) == 0
    # v1 against the v2 eval (tokenisation cases) fails below threshold
    code = execute_eval(
        "tool",
        ["catalog/word_count@v1"],
        eval_option=str(
            REPO_ROOT / "catalog/tools/word_count/v2/eval.yaml"
        ),
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "hyphenated_compound" in out  # named in Top failures


@pytest.mark.integration
def test_compare_tool_v1_v2_side_by_side_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """docs/03 gate 2: `foundry eval compare --tool word_count v1 v2`
    produces a side-by-side report with the per-case flips."""
    code = execute_eval("compare", ["v1", "v2"], tool="word_count")
    assert code == 0
    out = capsys.readouterr().out
    assert "v1" in out and "v2" in out
    assert "Score" in out and "Pass rate" in out
    assert "fail->pass" in out
    assert "hyphenated_compound" in out
    assert "punctuation_only" in out

    # both underlying eval runs persisted + one comparison artifact
    run_dirs = list(_runs_root(tmp_path).iterdir())
    comparison_files = [
        d / "eval_comparison.json"
        for d in run_dirs
        if (d / "eval_comparison.json").exists()
    ]
    assert len(comparison_files) == 1
    comparison = json.loads(comparison_files[0].read_text())
    assert comparison["labels"] == ["v1", "v2"]
    assert comparison["summary"]["fixes"] == 2
    assert comparison["summary"]["regressions"] == 0
    assert comparison["summary"]["score_b"] == 1.0
    # every run in the comparison shares ONE spec hash (docs/40 invariant 5)
    assert {run["eval_spec_hash"] for run in comparison["runs"]} == {
        comparison["eval_spec_hash"]
    }
    eval_results = [
        d for d in run_dirs if (d / "eval_result.json").exists()
    ]
    assert len(eval_results) == 2


@pytest.mark.integration
def test_compare_json_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = execute_eval(
        "compare", ["v1", "v2"], tool="word_count", json_output=True
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    deltas = {d["case_id"]: d for d in payload["deltas"]}
    assert deltas["hyphenated_compound"]["flip_direction"] == "fix"
    assert deltas["simple_sentence"]["flipped"] is False


@pytest.mark.integration
def test_compare_requires_two_versions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = execute_eval("compare", ["v1"], tool="word_count")
    assert code == 2
    assert ">= 2" in capsys.readouterr().err


@pytest.mark.integration
def test_unknown_tool_version_is_structured_exit_2(
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = execute_eval("tool", ["catalog/word_count@v9"])
    assert code == 2
    err = capsys.readouterr().err
    assert "v9" in err and "available" in err
