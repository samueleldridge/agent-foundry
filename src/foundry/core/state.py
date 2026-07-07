"""State primitives.

``StateBase`` is the base Pydantic model for user-defined state schemas.
Reducer annotations live in ``typing.Annotated[T, Reducer.X]`` and are
introspected by the compiler. See docs/10 § State primitives.
"""

from __future__ import annotations

from enum import StrEnum
from typing import get_type_hints

from pydantic import BaseModel, ConfigDict


class Reducer(StrEnum):
    APPEND = "append"
    MERGE = "merge"
    LAST_WRITE_WINS = "last_write_wins"
    REPLACE_IF_SET = "replace_if_set"


class StateBase(BaseModel):
    """Base for project state schemas.

    Fields default to ``Reducer.LAST_WRITE_WINS``. Use
    ``Annotated[..., Reducer.X]`` to override.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        validate_assignment=True,
    )

    @classmethod
    def reducer_map(cls) -> dict[str, Reducer]:
        hints = get_type_hints(cls, include_extras=True)
        out: dict[str, Reducer] = {}
        for name in cls.model_fields:
            hint = hints.get(name)
            reducer = Reducer.LAST_WRITE_WINS
            if hint is not None:
                metadata = getattr(hint, "__metadata__", ())
                for m in metadata:
                    if isinstance(m, Reducer):
                        reducer = m
                        break
            out[name] = reducer
        return out


__all__ = ["Reducer", "StateBase"]
