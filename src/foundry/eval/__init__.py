"""foundry.eval — the eval harness (docs/40).

Public API: ``run_eval`` (three scopes, one harness), the compare drivers,
the artifact read surface (``load_eval_result`` / ``list_eval_history`` —
what Phase 6's meta-agent consumes), and the typed schemas.
"""

from __future__ import annotations

from foundry.eval.compare import (
    WORKTREE_REF,
    compare_project_pin_sets,
    compare_runs,
    compare_tool_versions,
    write_comparison_artifact,
)
from foundry.eval.failure_clustering import (
    FailureCluster,
    FailureClustering,
    cluster_failures,
)
from foundry.eval.harness import (
    AgentEvalTarget,
    EvalTarget,
    ProjectEvalTarget,
    ToolEvalTarget,
    list_eval_history,
    load_eval_result,
    load_tool_target,
    run_eval,
    validate_cases,
    write_eval_artifact,
)
from foundry.eval.reporter import (
    comparison_json,
    render_comparison,
    render_result,
    result_json,
)
from foundry.eval.schemas import (
    CaseDelta,
    CaseResult,
    ComparisonSummary,
    EvalCase,
    EvalComparison,
    EvalRunResult,
    EvalScorer,
    EvalSpec,
    ScoredCase,
    ScorerConfig,
    ScorerSummary,
    eval_spec_hash,
)

__all__ = [
    "WORKTREE_REF",
    "AgentEvalTarget",
    "CaseDelta",
    "CaseResult",
    "ComparisonSummary",
    "EvalCase",
    "EvalComparison",
    "EvalRunResult",
    "EvalScorer",
    "EvalSpec",
    "EvalTarget",
    "FailureCluster",
    "FailureClustering",
    "ProjectEvalTarget",
    "ScoredCase",
    "ScorerConfig",
    "ScorerSummary",
    "ToolEvalTarget",
    "cluster_failures",
    "compare_project_pin_sets",
    "compare_runs",
    "compare_tool_versions",
    "comparison_json",
    "eval_spec_hash",
    "list_eval_history",
    "load_eval_result",
    "load_tool_target",
    "render_comparison",
    "render_result",
    "result_json",
    "run_eval",
    "validate_cases",
    "write_comparison_artifact",
    "write_eval_artifact",
]
