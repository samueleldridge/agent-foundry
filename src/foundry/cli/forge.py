"""`foundry forge <project>` — drive the meta-agent (docs/60, docs/62).

Autonomous CLI mode: streams forge progress lines to stdout as events
arrive, prints the final summary, exits 0 when the threshold was met and
1 on a best-effort/aborted termination (2 on configuration failure).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import BaseModel

from foundry.cli._helpers import print_foundry_error
from foundry.core.errors import (
    ConfigLoadError,
    ConfigValidationError,
    FoundryError,
)
from foundry.observability.logging import configure_logging


def _resolve_forge_project_dir(project: str) -> Path:
    """Like the shared resolver, but WITHOUT requiring system.yaml — a
    bootstrap-able project is an empty directory (foundry project new)."""
    candidates = [Path(project), Path("projects") / project]
    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()
    raise ConfigLoadError(
        f"project {project!r} not found; checked "
        f"{', '.join(str(c) for c in candidates)} — create it with "
        "`foundry project new <name>`",
        context={"project": project},
    )


def _parse_model(model: str | None) -> object | None:
    if model is None:
        return None
    from foundry.providers import ModelBinding, ModelSettings

    if "/" not in model:
        raise ConfigValidationError(
            f"--model must be '<provider>/<model>', got {model!r}",
            context={"model": model},
        )
    provider, model_name = model.split("/", 1)
    return ModelBinding(
        provider=provider,
        model=model_name,
        settings=ModelSettings(temperature=0.1, max_tokens=4096),
    )


def _progress_printer(event: BaseModel) -> None:
    kind = getattr(event, "event", "")
    if kind == "forge.started":
        print(
            f"[forge] forge_run_id: {getattr(event, 'forge_run_id', '?')} | "
            f"meta-agent: {getattr(event, 'meta_agent_version', '?')}"
        )
    elif kind == "forge.iteration_started":
        print(
            f"[forge] iteration {getattr(event, 'iteration_number', '?')} "
            f"({getattr(event, 'directive_kind', '?')})..."
        )
    elif kind == "forge.iteration_completed":
        score = getattr(event, "eval_score", None)
        delta = getattr(event, "eval_delta", None)
        bits = [
            f"[forge] iteration {getattr(event, 'iteration_number', '?')} "
            f"completed: score="
            + (f"{score:.3f}" if score is not None else "?")
        ]
        if delta is not None:
            bits.append(f"delta={delta:+.3f}")
        shas = getattr(event, "commit_shas", [])
        if shas:
            bits.append("commits=" + ",".join(s[:8] for s in shas))
        print(" ".join(bits))
    elif kind == "forge.rollback":
        print(
            f"[forge] rollback: {getattr(event, 'scope', '?')} "
            f"{getattr(event, 'target', '?')} -> "
            f"{getattr(event, 'to_version', '?')}"
        )
    elif kind == "forge.terminated":
        print(f"[forge] terminated: {getattr(event, 'reason', '?')}")


def execute_forge(
    project: str,
    *,
    description: str,
    eval_path: str,
    threshold: float = 0.9,
    max_iter: int = 5,
    max_cost_usd: str | None = None,
    model: str | None = None,
    no_improvement_after: int = 3,
    quiet: bool = False,
) -> int:
    """The `foundry forge` implementation. Returns the exit code."""
    configure_logging()
    try:
        from foundry.configurator import (
            ForgeGuardrails,
            ForgeSession,
            MetaAgent,
            render_summary,
        )
        from foundry.providers import ModelBinding

        project_dir = _resolve_forge_project_dir(project)
        cost_cap: Decimal | None = None
        if max_cost_usd is not None:
            try:
                cost_cap = Decimal(max_cost_usd)
            except InvalidOperation as exc:
                raise ConfigValidationError(
                    f"--max-cost-usd must be a decimal amount, got "
                    f"{max_cost_usd!r}",
                    context={"max_cost_usd": max_cost_usd},
                    cause=exc,
                ) from exc
        binding = _parse_model(model)
        assert binding is None or isinstance(binding, ModelBinding)
        agent = MetaAgent(
            project_dir.name,
            projects_root=project_dir.parent,
            model_binding=binding,
            guardrails=ForgeGuardrails(
                max_iter=max_iter,
                max_cost_usd=cost_cap,
                no_improvement_after=no_improvement_after,
            ),
        )
        session = ForgeSession(
            meta_agent=agent,
            description=description,
            eval_spec_path=Path(eval_path),
            threshold=threshold,
            event_sink=None if quiet else _progress_printer,
        )
        result = asyncio.run(session.run())
        print()
        print(render_summary(result))
        print(f"Trajectory artifact: {result.artifact_dir}")
        return 0 if result.threshold_met else 1
    except FoundryError as exc:
        print_foundry_error(exc)
        return 2


__all__ = ["execute_forge"]
