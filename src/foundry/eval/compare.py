"""Cross-version + cross-pin-set comparison (docs/40 § EvalComparison).

Two async drivers plus one pure function:

- ``compare_tool_versions`` — the same standalone eval run against N
  versions of one tool.
- ``compare_project_pin_sets`` — the same project eval run against the
  project as it existed at N git refs ("pin sets"), each materialized into
  a temporary overlay via ``git archive`` (READ-only: no pin-writing
  helpers — those are Phase 5).
- ``compare_runs`` — EvalRunResults → EvalComparison with per-case deltas,
  flip detection, and per-agent score breakdowns.

Invariant (docs/40 #5): comparison is only valid across runs of the SAME
``eval_spec_hash`` — for tool compare the eval spec of the NEWEST version
listed is used against every version; for pin-set compare the spec is
loaded from the CURRENT working tree.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
import tempfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from foundry.config import FoundryRoots, load_eval_spec
from foundry.config.secrets import SecretsProvider
from foundry.core import RunId
from foundry.core.errors import CompileError, ConfigValidationError, VersioningError
from foundry.eval.harness import (
    ProjectEvalTarget,
    ToolEvalTarget,
    load_tool_target,
    run_eval,
)
from foundry.eval.schemas import (
    CaseDelta,
    ComparisonSummary,
    EvalComparison,
    EvalRunResult,
)
from foundry.runtime.execution import EventSink
from foundry.storage.paths import run_dir

WORKTREE_REF = "worktree"
"""Special pin-set ref: the live working tree instead of a git ref."""


# --- pure comparison ---------------------------------------------------------------


def compare_runs(
    runs: list[EvalRunResult], labels: list[str]
) -> EvalComparison:
    """Build the EvalComparison from N runs of the same eval spec. The
    summary compares the FIRST and LAST runs; deltas carry all N scores."""
    if len(runs) < 2 or len(runs) != len(labels):
        raise ConfigValidationError(
            f"comparison needs >= 2 runs with one label each; got "
            f"{len(runs)} run(s) / {len(labels)} label(s)",
            context={"runs": len(runs), "labels": labels},
        )
    hashes = sorted({r.eval_spec_hash for r in runs})
    if len(hashes) > 1:
        raise ConfigValidationError(
            "cannot compare runs of DIFFERENT eval specs (docs/40 invariant "
            f"5); spec hashes: {', '.join(hashes)}",
            context={"spec_hashes": hashes},
        )

    per_run = [{c.case_id: c for c in run.per_case} for run in runs]
    case_ids = [c.case_id for c in runs[0].per_case]
    for run in runs[1:]:
        case_ids.extend(
            c.case_id for c in run.per_case if c.case_id not in case_ids
        )

    deltas: list[CaseDelta] = []
    regressions = 0
    fixes = 0
    for case_id in case_ids:
        scores = [
            (mapping[case_id].score if case_id in mapping else 0.0)
            for mapping in per_run
        ]
        first = per_run[0].get(case_id)
        last = per_run[-1].get(case_id)
        first_pass = first.pass_ if first is not None else False
        last_pass = last.pass_ if last is not None else False
        flipped = first_pass != last_pass
        direction: Any = None
        if flipped:
            direction = "regression" if first_pass else "fix"
            regressions += int(direction == "regression")
            fixes += int(direction == "fix")
        deltas.append(
            CaseDelta(
                case_id=case_id,
                scores=scores,
                delta=scores[-1] - scores[0],
                flipped=flipped,
                flip_direction=direction,
            )
        )

    agent_names: list[str] = []
    for run in runs:
        for name in run.metadata.get("per_agent", {}):
            if name not in agent_names:
                agent_names.append(name)
    per_agent = {
        name: [
            float(run.metadata.get("per_agent", {}).get(name, 0.0))
            for run in runs
        ]
        for name in agent_names
    }

    summary = ComparisonSummary(
        label_a=labels[0],
        label_b=labels[-1],
        score_a=runs[0].score,
        score_b=runs[-1].score,
        delta=runs[-1].score - runs[0].score,
        regressions=regressions,
        fixes=fixes,
        cost_a_usd=runs[0].cost_total_usd,
        cost_b_usd=runs[-1].cost_total_usd,
        per_agent=per_agent,
    )
    return EvalComparison(
        eval_spec_hash=runs[0].eval_spec_hash,
        labels=list(labels),
        runs=runs,
        deltas=deltas,
        summary=summary,
    )


def write_comparison_artifact(comparison: EvalComparison) -> Path:
    """Persist the comparison under its own run directory:
    ``~/.foundry/runs/<id>/eval_comparison.json`` (docs/03 § Phase 4:
    'produces an EvalComparison artifact')."""
    directory = run_dir(str(RunId.new()))
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "eval_comparison.json").write_text(
        comparison.model_dump_json(indent=2, by_alias=True) + "\n"
    )
    return directory


# --- tool cross-version ---------------------------------------------------------------


async def compare_tool_versions(
    name_or_ref: str,
    versions: list[str],
    roots: FoundryRoots,
    *,
    eval_path: Path | None = None,
    secrets: SecretsProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    event_sink: EventSink | None = None,
) -> EvalComparison:
    """Run ONE eval spec against N versions of a tool. The spec is the
    standalone eval of the NEWEST (last-listed) version unless ``eval_path``
    overrides — a single ``eval_spec_hash`` across every run (docs/40
    invariant 5); the spec's pinned ``target`` version is informational."""
    if len(versions) < 2:
        raise ConfigValidationError(
            f"tool comparison needs >= 2 versions; got {versions}",
            context={"versions": versions},
        )
    base = name_or_ref if "/" in name_or_ref else f"catalog/{name_or_ref}"
    targets = [load_tool_target(base, roots, version=v) for v in versions]

    spec_path = eval_path or _standalone_eval_path(targets[-1])
    spec = load_eval_spec(spec_path)

    runs: list[EvalRunResult] = []
    for target in targets:
        runs.append(
            await run_eval(
                spec,
                target,
                secrets=secrets,
                transport=transport,
                event_sink=event_sink,
                eval_spec_ref=str(spec_path),
            )
        )
    return compare_runs(runs, versions)


def _standalone_eval_path(target: ToolEvalTarget) -> Path:
    rel = target.loaded.spec.standalone_eval
    if rel is None:
        raise CompileError(
            f"tool {target.ref!r} declares no standalone eval "
            "(standalone_eval: null); pass --eval <path> to compare",
            context={"ref": target.ref},
        )
    return target.loaded.directory / rel


# --- project cross-pin-set --------------------------------------------------------------


async def compare_project_pin_sets(
    project_dir: Path,
    eval_path: Path,
    pin_set_refs: list[str],
    *,
    secrets: SecretsProvider | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    event_sink: EventSink | None = None,
    meta_authored: bool = False,
) -> EvalComparison:
    """Run ONE project eval against the project as it existed at each git
    ref. Each ref's project subtree (plus the repo ``catalog/`` when
    present at that ref) is materialized read-only into a temp overlay via
    ``git archive`` and compiled from there; the eval spec comes from the
    CURRENT tree so every run shares one spec hash. The special ref
    ``worktree`` compiles the live project directory instead.
    ``meta_authored=True`` (the forge's compare_versions wrapper) makes
    every compile reject meta-forbidden ``provider_overrides``."""
    from foundry.orchestration.compiler import compile_project

    if len(pin_set_refs) < 2:
        raise ConfigValidationError(
            f"pin-set comparison needs >= 2 --pin-set refs; got "
            f"{pin_set_refs}",
            context={"pin_sets": pin_set_refs},
        )
    spec = load_eval_spec(eval_path)
    project_dir = project_dir.resolve()

    runs: list[EvalRunResult] = []
    with tempfile.TemporaryDirectory(prefix="foundry-pinset-") as tmp:
        for index, ref in enumerate(pin_set_refs):
            if ref == WORKTREE_REF:
                materialized = project_dir
            else:
                dest = Path(tmp) / f"pinset_{index}"
                materialized = _materialize_ref(project_dir, ref, dest)
            compiled = compile_project(
                materialized,
                secrets=secrets,
                transport=transport,
                meta_authored=meta_authored,
            )
            result = await run_eval(
                spec,
                ProjectEvalTarget(compiled),
                secrets=secrets,
                transport=transport,
                event_sink=event_sink,
                eval_spec_ref=str(eval_path),
                extra_metadata={"pin_set_ref": ref},
            )
            runs.append(result)
    return compare_runs(runs, pin_set_refs)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise VersioningError(
            f"git {' '.join(args[:2])} failed: "
            f"{result.stderr.decode(errors='replace').strip()}",
            context={"args": list(args),
                     "returncode": result.returncode},
        )
    return result.stdout.decode(errors="replace").strip()


def _git_bytes(repo: Path, *args: str) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        raise VersioningError(
            f"git {' '.join(args[:2])} failed: "
            f"{result.stderr.decode(errors='replace').strip()}",
            context={"args": list(args),
                     "returncode": result.returncode},
        )
    return result.stdout


def _materialize_ref(project_dir: Path, ref: str, dest: Path) -> Path:
    """Extract the project subtree (and ``catalog/`` when present at the
    ref) into ``dest`` — a read-only overlay; the working tree is never
    touched (pin WRITE paths are Phase 5)."""
    repo_root = Path(_git(project_dir, "rev-parse", "--show-toplevel"))
    prefix = _git(project_dir, "rev-parse", "--show-prefix").rstrip("/")
    if not prefix:
        raise VersioningError(
            f"project directory {project_dir} IS the repo root; pin-set "
            "comparison expects projects/<name> inside a repo",
            context={"project_dir": str(project_dir)},
        )
    paths = [prefix]
    probe = subprocess.run(
        ["git", "-C", str(repo_root), "cat-file", "-e", f"{ref}:catalog"],
        capture_output=True,
        timeout=30,
        check=False,
    )
    if probe.returncode == 0:
        paths.append("catalog")
    archive = _git_bytes(repo_root, "archive", ref, "--", *paths)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(dest, filter="data")
    materialized = dest / prefix
    if not materialized.is_dir():
        raise VersioningError(
            f"pin-set ref {ref!r} does not contain {prefix!r}",
            context={"ref": ref, "prefix": prefix},
        )
    return materialized


# --- comparison total cost (report convenience) --------------------------------------


def total_cost(comparison: EvalComparison) -> Decimal | None:
    costs = [
        run.cost_total_usd
        for run in comparison.runs
        if run.cost_total_usd is not None
    ]
    return sum(costs, Decimal("0")) if costs else None


__all__ = [
    "WORKTREE_REF",
    "compare_project_pin_sets",
    "compare_runs",
    "compare_tool_versions",
    "total_cost",
    "write_comparison_artifact",
]
