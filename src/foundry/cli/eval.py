"""`foundry eval` — run evals and compare versions (docs/40 § CLI surface).

One dispatcher covers the whole surface so the documented shapes hold:

    foundry eval <project> <eval-set> [--fail-under N] [--json]
    foundry eval tool <ref>@<version> [--eval <path>]
    foundry eval agent <project> <agent> [--eval <name>]
    foundry eval compare --tool <name> <v1> <v2> [...]
    foundry eval compare --project <path> --pin-set <ref> --pin-set <ref> [--eval <path>]
    foundry eval show <eval_run_id>
    foundry eval list <project>

Exit codes (docs/40 § CI integration, stable): 0 = pass, 1 = score below
threshold (or --fail-under), 2 = infrastructure/config failure. A run whose
non-skipped cases ALL errored is reported as infrastructure failure (2) so
CI can tell "the system is wrong" from "the eval could not run".
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx

from foundry.cli._helpers import resolve_project_dir
from foundry.config import FoundryRoots, load_eval_spec
from foundry.core.errors import ConfigLoadError, ConfigValidationError, FoundryError
from foundry.eval import (
    AgentEvalTarget,
    EvalRunResult,
    ProjectEvalTarget,
    compare_project_pin_sets,
    compare_tool_versions,
    comparison_json,
    list_eval_history,
    load_eval_result,
    load_tool_target,
    render_comparison,
    render_result,
    result_json,
    run_eval,
    write_comparison_artifact,
)
from foundry.observability.logging import configure_logging, run_logger


def execute_eval(
    target: str,
    args: list[str],
    *,
    fail_under: float | None = None,
    json_output: bool = False,
    tool: str | None = None,
    project: str | None = None,
    pin_sets: list[str] | None = None,
    eval_option: str | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """The `foundry eval` implementation. Returns the process exit code."""
    configure_logging()
    try:
        if target == "tool":
            return _eval_tool(
                args, fail_under, json_output, eval_option, transport,
                connections_from=project,
            )
        if target == "agent":
            return _eval_agent(args, fail_under, json_output, eval_option, transport)
        if target == "compare":
            return _compare(
                args, tool, project, pin_sets or [], eval_option,
                json_output, transport,
            )
        if target == "show":
            return _show(args, json_output)
        if target == "list":
            return _list(args)
        return _eval_project(
            target, args, fail_under, json_output, transport
        )
    except FoundryError as exc:
        _print_error(exc)
        return 2


def _print_error(exc: FoundryError) -> None:
    import sys

    print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
    interesting = {
        k: v
        for k, v in exc.context.items()
        if v is not None and k not in ("file", "pointer", "line", "column")
        and f"{k}:" not in str(exc)
    }
    for key, value in interesting.items():
        print(f"  {key}: {value}", file=sys.stderr)


# --- run commands -------------------------------------------------------------------


def _finish_run(
    result: EvalRunResult, fail_under: float | None, json_output: bool
) -> int:
    print(result_json(result) if json_output else render_result(result))
    run_logger(str(result.eval_run_id)).info(
        "eval.completed",
        eval_name=result.eval_name,
        score=result.score,
        passed=result.passed,
        artifact_dir=result.metadata.get("artifact_dir"),
    )
    runnable = [c for c in result.per_case if c.status != "skipped"]
    if runnable and all(c.status == "error" for c in runnable):
        # Nothing actually scored — infrastructure failure, not a quality
        # verdict (docs/40: exit 2 on infrastructure failure).
        return 2
    floor = fail_under if fail_under is not None else result.threshold
    return 0 if result.score >= floor else 1


def _eval_project(
    project_path: str,
    args: list[str],
    fail_under: float | None,
    json_output: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> int:
    from foundry.orchestration.compiler import compile_project

    if len(args) != 1:
        raise ConfigValidationError(
            "usage: foundry eval <project> <eval-set>",
            context={"received_args": [project_path, *args]},
        )
    project_dir = resolve_project_dir(project_path)
    spec_path = _resolve_eval_path(project_dir, args[0])
    spec = load_eval_spec(spec_path)
    compiled = compile_project(project_dir, transport=transport)
    result = asyncio.run(
        run_eval(
            spec,
            ProjectEvalTarget(compiled),
            transport=transport,
            eval_spec_ref=str(spec_path),
        )
    )
    return _finish_run(result, fail_under, json_output)


def _eval_agent(
    args: list[str],
    fail_under: float | None,
    json_output: bool,
    eval_option: str | None,
    transport: httpx.AsyncBaseTransport | None,
) -> int:
    from foundry.orchestration.compiler import compile_project

    if len(args) != 2:
        raise ConfigValidationError(
            "usage: foundry eval agent <project> <agent> [--eval <name>]",
            context={"received_args": args},
        )
    project_dir, agent_name = Path(args[0]), args[1]
    compiled = compile_project(project_dir, transport=transport)
    known = compiled.project.system.agents
    if agent_name not in known:
        raise ConfigValidationError(
            f"agent {agent_name!r} is not in the project; known: "
            f"{', '.join(known)}",
            context={"agent": agent_name, "known_agents": known},
        )
    if agent_name != compiled.agent_name:
        raise ConfigValidationError(
            f"agent {agent_name!r} is not the project's flow agent "
            f"({compiled.agent_name!r}); multi-agent eval targets land with "
            "Phase 7",
            context={"agent": agent_name, "flow_agent": compiled.agent_name},
        )
    spec_path = _agent_eval_path(project_dir, agent_name, eval_option)
    spec = load_eval_spec(spec_path)
    result = asyncio.run(
        run_eval(
            spec,
            AgentEvalTarget(compiled),
            transport=transport,
            eval_spec_ref=str(spec_path),
        )
    )
    return _finish_run(result, fail_under, json_output)


def _eval_tool(
    args: list[str],
    fail_under: float | None,
    json_output: bool,
    eval_option: str | None,
    transport: httpx.AsyncBaseTransport | None,
    *,
    connections_from: str | None = None,
) -> int:
    if len(args) != 1:
        raise ConfigValidationError(
            "usage: foundry eval tool <ref>@<version> [--project <dir>] "
            "(--project lends a binding project's connections to a "
            "connection-requiring tool)",
            context={"received_args": args},
        )
    ref = args[0] if "/" in args[0] else f"catalog/{args[0]}"
    project_dir = Path(connections_from) if connections_from else None
    roots = FoundryRoots.for_project(project_dir or Path.cwd())
    target = load_tool_target(ref, roots, connections_from=project_dir)
    if eval_option is not None:
        spec_path = Path(eval_option)
    else:
        rel = target.loaded.spec.standalone_eval
        if rel is None:
            raise ConfigValidationError(
                f"tool {target.ref!r} declares no standalone eval "
                "(standalone_eval: null); pass --eval <path>",
                context={"ref": target.ref},
            )
        spec_path = target.loaded.directory / rel
    spec = load_eval_spec(spec_path)
    result = asyncio.run(
        run_eval(
            spec, target, transport=transport, eval_spec_ref=str(spec_path)
        )
    )
    return _finish_run(result, fail_under, json_output)


# --- compare -------------------------------------------------------------------------


def _compare(
    args: list[str],
    tool: str | None,
    project: str | None,
    pin_sets: list[str],
    eval_option: str | None,
    json_output: bool,
    transport: httpx.AsyncBaseTransport | None,
) -> int:
    if (tool is None) == (project is None):
        raise ConfigValidationError(
            "usage: foundry eval compare --tool <name> <v1> <v2> [...] | "
            "foundry eval compare --project <path> --pin-set <a> "
            "--pin-set <b> [--eval <path>]",
            context={"tool": tool, "project": project},
        )
    if tool is not None:
        if len(args) < 2:
            raise ConfigValidationError(
                f"compare --tool needs >= 2 versions; got {args}",
                context={"versions": args},
            )
        comparison = asyncio.run(
            compare_tool_versions(
                tool,
                args,
                FoundryRoots.for_project(Path.cwd()),
                eval_path=Path(eval_option) if eval_option else None,
                transport=transport,
            )
        )
    else:
        assert project is not None
        project_dir = Path(project)
        eval_path = _resolve_eval_path(
            project_dir, eval_option or _default_project_eval(project_dir)
        )
        comparison = asyncio.run(
            compare_project_pin_sets(
                project_dir, eval_path, pin_sets, transport=transport
            )
        )
    artifact_dir = write_comparison_artifact(comparison)
    if json_output:
        print(comparison_json(comparison))
    else:
        print(render_comparison(comparison))
        print(f"\nComparison artifact: {artifact_dir}/eval_comparison.json")
    return 0


# --- show / list -----------------------------------------------------------------------


def _show(args: list[str], json_output: bool) -> int:
    if len(args) != 1:
        raise ConfigValidationError(
            "usage: foundry eval show <eval_run_id>",
            context={"received_args": args},
        )
    result = load_eval_result(args[0])
    print(result_json(result) if json_output else render_result(result))
    return 0


def _list(args: list[str]) -> int:
    if len(args) != 1:
        raise ConfigValidationError(
            "usage: foundry eval list <project>",
            context={"received_args": args},
        )
    entries = list_eval_history(Path(args[0]))
    if not entries:
        print("(no eval history)")
        return 0
    for entry in entries:
        status = "PASS" if entry.get("passed") else "FAIL"
        print(
            f"{entry.get('completed_at', ''):<27} "
            f"{entry.get('eval_run_id', ''):<28} "
            f"{entry.get('eval_name', ''):<32} "
            f"score {entry.get('score', 0.0):.2f}  {status}"
        )
    return 0


# --- path resolution --------------------------------------------------------------------


def _resolve_eval_path(project_dir: Path, eval_set: str) -> Path:
    candidates = [
        Path(eval_set),
        project_dir / eval_set,
        project_dir / "evals" / f"{eval_set}.yaml",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise ConfigLoadError(
        f"eval set {eval_set!r} not found; checked: "
        f"{', '.join(str(c) for c in candidates)}",
        context={"eval_set": eval_set,
                 "checked": [str(c) for c in candidates]},
    )


def _default_project_eval(project_dir: Path) -> str:
    evals_dir = project_dir / "evals"
    found = sorted(evals_dir.glob("*.yaml")) if evals_dir.is_dir() else []
    if len(found) != 1:
        raise ConfigValidationError(
            f"project has {len(found)} eval set(s) under {evals_dir}; pass "
            "--eval <path> to pick one",
            context={"evals_dir": str(evals_dir),
                     "found": [f.name for f in found]},
        )
    return str(found[0])


def _agent_eval_path(
    project_dir: Path, agent_name: str, eval_option: str | None
) -> Path:
    eval_dir = project_dir / "agents" / agent_name / "eval"
    if eval_option is not None:
        candidate = Path(eval_option)
        if candidate.is_file():
            return candidate
        named = eval_dir / f"{eval_option}.yaml"
        if named.is_file():
            return named
        raise ConfigLoadError(
            f"agent eval {eval_option!r} not found (checked {candidate} "
            f"and {named})",
            context={"eval": eval_option},
        )
    found = sorted(eval_dir.glob("*.yaml")) if eval_dir.is_dir() else []
    if len(found) != 1:
        raise ConfigValidationError(
            f"agent {agent_name!r} has {len(found)} eval set(s) under "
            f"{eval_dir}; pass --eval <name> to pick one",
            context={"eval_dir": str(eval_dir),
                     "found": [f.name for f in found]},
        )
    return found[0]


__all__ = ["execute_eval"]
