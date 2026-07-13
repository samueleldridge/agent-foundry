"""State-model test helpers (docs/82 § `make_state` + `assert_state_transition`).

Builds real compiled state models from a project's ``state.yaml`` so tests
exercise the same Pydantic validation + reducer semantics the runtime uses.
This module MUST NOT import pytest.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from foundry.config import load_state_spec
from foundry.core.state import StateBase
from foundry.orchestration.state_scope import CompiledState, compile_state
from foundry.runtime.execution import apply_delta

_MISSING = object()


def _compile(spec_path: str | Path) -> CompiledState:
    path = Path(spec_path)
    spec = load_state_spec(path)
    # Visibility entries name every node declared in the spec; compile_state
    # only uses node_names for visibility validation, so the spec's own set
    # is exactly right here.
    return compile_state(spec, sorted(spec.visibility), where=str(path))


def make_state(spec_path: str | Path, **field_values: Any) -> StateBase:
    """Load ``state.yaml``, compile the state model, and construct an
    instance from ``field_values``. Pydantic validates; a shape mismatch
    raises ``pydantic.ValidationError``."""
    return _compile(spec_path).model(**field_values)


class StateBuilder:
    """Fluent alternative to ``make_state`` for incremental construction."""

    def __init__(self, spec_path: str | Path) -> None:
        self._spec_path = spec_path
        self._values: dict[str, Any] = {}

    def set(self, field: str, value: Any) -> StateBuilder:
        self._values[field] = value
        return self

    def build(self) -> StateBase:
        return make_state(self._spec_path, **self._values)


def _as_dict(state: StateBase | dict[str, Any]) -> dict[str, Any]:
    if isinstance(state, StateBase):
        # Not model_dump(): keep field values as live objects (e.g.
        # FoundryMessage instances) so expected_final can compare directly.
        return {name: getattr(state, name) for name in type(state).model_fields}
    return dict(state)


def assert_state_transition(
    spec_path: str | Path,
    initial: StateBase | dict[str, Any],
    deltas: list[dict[str, Any]],
    expected_final: dict[str, Any],
) -> None:
    """Apply ``deltas`` in order through the spec's compiled reducers and
    compare the fields named in ``expected_final`` (subset comparison).
    Raises ``AssertionError`` with a per-field diff on mismatch."""
    compiled = _compile(spec_path)
    state = _as_dict(initial)
    for delta in deltas:
        state = apply_delta(state, delta, compiled.reducers)

    mismatches: list[str] = []
    for field_name, expected in expected_final.items():
        actual = state.get(field_name, _MISSING)
        if actual is _MISSING:
            mismatches.append(
                f"  {field_name}: expected {expected!r}, but the field is unset"
            )
        elif actual != expected:
            mismatches.append(
                f"  {field_name}:\n"
                f"    expected: {expected!r}\n"
                f"    actual:   {actual!r}"
            )
    if mismatches:
        raise AssertionError(
            f"state transition mismatch after {len(deltas)} delta(s) "
            f"({spec_path}):\n" + "\n".join(mismatches)
        )


__all__ = ["StateBuilder", "assert_state_transition", "make_state"]
