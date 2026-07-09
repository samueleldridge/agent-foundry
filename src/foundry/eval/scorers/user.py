"""User-plugged scorer discovery via Python entry points (docs/40 § user).

A project registers scorers in its own pyproject.toml::

    [project.entry-points."foundry.scorers"]
    business_specific_scorer = "my_project.scorers:BusinessSpecificScorer"

An eval spec then references the entry-point NAME::

    scorers:
      - kind: user
        name: business_specific_scorer
        config: { ... passed to score() ... }

The loaded object must satisfy the ``Scorer`` protocol (a ``name`` attr and
``async def score(self, case, actual, config) -> ScoredCase``). User scorers
respect the same async + failure contracts as the built-ins: a raise during
scoring records 0.0 for that scorer and the run continues.
"""

from __future__ import annotations

import inspect
from importlib.metadata import entry_points

from foundry.core.errors import ConfigValidationError
from foundry.eval.schemas import ScorerConfig
from foundry.eval.scorers._common import Scorer, ScorerServices

_GROUP = "foundry.scorers"


def load_user_scorer(config: ScorerConfig, services: ScorerServices) -> Scorer:
    """Resolve ``kind: user`` scorers: the ScorerConfig.name is the
    entry-point name in the ``foundry.scorers`` group. Fails at load with
    the list of names that ARE installed."""
    matches = entry_points(group=_GROUP, name=config.name)
    if not matches:
        available = sorted(ep.name for ep in entry_points(group=_GROUP))
        raise ConfigValidationError(
            f"user scorer {config.name!r} not found in entry-point group "
            f"{_GROUP!r}; installed: {', '.join(available) or '(none)'}",
            context={"scorer": config.name, "group": _GROUP,
                     "installed": available},
        )
    loaded = next(iter(matches)).load()
    scorer = loaded() if inspect.isclass(loaded) else loaded
    score = getattr(scorer, "score", None)
    if score is None or not inspect.iscoroutinefunction(score):
        raise ConfigValidationError(
            f"user scorer {config.name!r} does not satisfy the Scorer "
            "protocol: it needs `async def score(self, case, actual, "
            "config) -> ScoredCase`",
            context={"scorer": config.name,
                     "loaded_type": type(scorer).__name__},
        )
    if not getattr(scorer, "name", ""):
        scorer.name = config.name
    return scorer  # type: ignore[no-any-return]


__all__ = ["load_user_scorer"]
