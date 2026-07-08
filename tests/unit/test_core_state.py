"""StateBase reducer introspection tests (docs/10 § State primitives)."""

from __future__ import annotations

from typing import Annotated, Any

import pytest
from pydantic import Field

from foundry.core import FoundryMessage, Reducer, StateBase


class _MyState(StateBase):
    messages: Annotated[list[FoundryMessage], Reducer.APPEND] = Field(
        default_factory=list
    )
    scratchpad: Annotated[dict[str, Any], Reducer.MERGE] = Field(default_factory=dict)
    result: Annotated[str | None, Reducer.REPLACE_IF_SET] = None
    final: str | None = None  # implicit LAST_WRITE_WINS


@pytest.mark.unit
def test_reducer_map_reads_annotations() -> None:
    assert _MyState.reducer_map() == {
        "messages": Reducer.APPEND,
        "scratchpad": Reducer.MERGE,
        "result": Reducer.REPLACE_IF_SET,
        "final": Reducer.LAST_WRITE_WINS,
    }


@pytest.mark.unit
def test_unannotated_fields_default_to_last_write_wins() -> None:
    class Bare(StateBase):
        a: int = 0

    assert Bare.reducer_map() == {"a": Reducer.LAST_WRITE_WINS}


# --- apply_reducer semantics (docs/22 § Reducers) ----------------------------


@pytest.mark.unit
def test_append_concatenates_preserving_order() -> None:
    from foundry.core import apply_reducer

    assert apply_reducer(Reducer.APPEND, [1, 2], [3]) == [1, 2, 3]
    assert apply_reducer(Reducer.APPEND, None, [1]) == [1]


@pytest.mark.unit
def test_merge_shallow_merges_dicts_incoming_wins() -> None:
    from foundry.core import apply_reducer

    assert apply_reducer(Reducer.MERGE, {"a": 1}, {"a": 2, "b": 3}) == {
        "a": 2,
        "b": 3,
    }
    assert apply_reducer(Reducer.MERGE, None, {"x": 1}) == {"x": 1}


@pytest.mark.unit
def test_merge_unions_sets() -> None:
    from foundry.core import apply_reducer

    assert apply_reducer(Reducer.MERGE, {1, 2}, {2, 3}) == {1, 2, 3}


@pytest.mark.unit
def test_last_write_wins_replaces() -> None:
    from foundry.core import apply_reducer

    assert apply_reducer(Reducer.LAST_WRITE_WINS, "old", "new") == "new"
    assert apply_reducer(Reducer.LAST_WRITE_WINS, "old", None) is None


@pytest.mark.unit
def test_replace_if_set_preserves_value_against_none_write() -> None:
    from foundry.core import apply_reducer

    assert apply_reducer(Reducer.REPLACE_IF_SET, "decided", None) == "decided"
    assert apply_reducer(Reducer.REPLACE_IF_SET, "decided", "changed") == "changed"
    assert apply_reducer(Reducer.REPLACE_IF_SET, None, "first") == "first"
