"""FOUNDRY_FORGE_MAX_ITER — the global default iteration cap consumed
when `foundry forge --max-iter` / the studio forge route's ``max_iter``
is omitted (docs/60 § Safety guards)."""

from __future__ import annotations

import pytest

from foundry.configurator import forge_max_iter_default
from foundry.core.errors import ConfigValidationError
from foundry.studio.schemas import ForgeLaunchRequest


@pytest.mark.unit
def test_default_is_five(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FOUNDRY_FORGE_MAX_ITER", raising=False)
    assert forge_max_iter_default() == 5


@pytest.mark.unit
def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FOUNDRY_FORGE_MAX_ITER", "12")
    assert forge_max_iter_default() == 12


@pytest.mark.unit
@pytest.mark.parametrize("raw", ["three", "0", "101", "-1"])
def test_invalid_env_is_an_error_not_a_silent_fallback(
    monkeypatch: pytest.MonkeyPatch, raw: str
) -> None:
    monkeypatch.setenv("FOUNDRY_FORGE_MAX_ITER", raw)
    with pytest.raises(ConfigValidationError):
        forge_max_iter_default()


@pytest.mark.unit
def test_forge_launch_request_defaults_max_iter_to_none() -> None:
    """The route distinguishes "omitted" (env default applies) from an
    explicit value — CLI parity with the optional --max-iter flag."""
    body = ForgeLaunchRequest(
        project="p", description="d", eval_path="projects/p/evals/e.yaml"
    )
    assert body.max_iter is None
