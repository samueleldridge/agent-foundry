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
