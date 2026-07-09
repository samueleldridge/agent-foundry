"""Scorer protocol, runtime services, and the ScorerRegistry (docs/40).

Lives in ``_common`` (not ``__init__``) so the concrete scorer modules can
import the shared surface without a circular package import; the package
``__init__`` re-exports everything and registers the built-ins.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import httpx
from pydantic import BaseModel, ValidationError

from foundry.config import EnvSecretsProvider
from foundry.config.secrets import SecretsProvider
from foundry.core import Session
from foundry.core.errors import ConfigValidationError
from foundry.core.tool import EmitFn
from foundry.eval.schemas import EvalCase, ScoredCase, ScorerConfig


@dataclass
class ScorerServices:
    """Runtime plumbing scorers may need (only ``llm_judge`` uses it today).

    ``judge_session`` is the EVAL-scoped session: judge calls are charged
    against the eval's total cost budget and their events carry the
    eval_run_id (docs/40 § llm_judge: 'judge calls show up in audit just
    like any other LLM call')."""

    secrets: SecretsProvider = field(default_factory=EnvSecretsProvider)
    transport: httpx.AsyncBaseTransport | None = None
    emit: EmitFn | None = None
    judge_session: Session | None = None
    deterministic: bool = True
    seed: int | None = None


@runtime_checkable
class Scorer(Protocol):
    """The docs/40 scorer contract. ``config`` is the raw ScorerConfig.config
    dict; built-in scorers validate it once at construction and ignore the
    per-call copy, user scorers consume it directly."""

    name: str

    async def score(
        self, case: EvalCase, actual: Any, config: dict[str, Any]
    ) -> ScoredCase: ...


ScorerFactory = Callable[[ScorerConfig, ScorerServices], Scorer]


class ScorerRegistry:
    """kind string → scorer factory (docs/40 § Scorers)."""

    def __init__(self) -> None:
        self._factories: dict[str, ScorerFactory] = {}

    def register(self, kind: str, factory: ScorerFactory) -> None:
        self._factories[kind] = factory

    def kinds(self) -> list[str]:
        return sorted(self._factories)

    def create(self, config: ScorerConfig, services: ScorerServices) -> Scorer:
        factory = self._factories.get(config.kind)
        if factory is None:
            raise ConfigValidationError(
                f"unknown scorer kind {config.kind!r} for scorer "
                f"{config.name!r}; known: {', '.join(self.kinds())}",
                context={"scorer": config.name, "kind": config.kind,
                         "known_kinds": self.kinds()},
            )
        return factory(config, services)


def parse_scorer_config[ModelT: BaseModel](
    model: type[ModelT], config: ScorerConfig
) -> ModelT:
    """Validate a ScorerConfig.config dict against the kind's config model —
    at scorer-build time, before any case runs (docs/40 failure mode:
    invalid scorer config → ConfigValidationError at load)."""
    try:
        return model.model_validate(config.config)
    except ValidationError as exc:
        first = exc.errors()[0]
        raise ConfigValidationError(
            f"scorer {config.name!r} ({config.kind}) has invalid config: "
            f"{first['msg']} (at {'/'.join(str(p) for p in first['loc'])})",
            context={
                "scorer": config.name,
                "kind": config.kind,
                "errors": [
                    {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]}
                    for e in exc.errors()
                ],
            },
            cause=exc,
        ) from exc


def resolve_path(obj: Any, path: str | None) -> tuple[bool, Any]:
    """Dotted-path lookup into nested dicts/lists ('investigation.confidence',
    'items.0.id'). Returns (found, value); (True, obj) for an empty path."""
    if not path:
        return True, obj
    current = obj
    for part in path.split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif (
            isinstance(current, list)
            and part.lstrip("-").isdigit()
            and -len(current) <= int(part) < len(current)
        ):
            current = current[int(part)]
        else:
            return False, None
    return True, current


__all__ = [
    "Scorer",
    "ScorerFactory",
    "ScorerRegistry",
    "ScorerServices",
    "parse_scorer_config",
    "resolve_path",
]
