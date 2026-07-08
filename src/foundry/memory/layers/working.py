"""Working memory: a recency window over a conversation state field.

Stateless by design (docs/26 invariant 5): the state field is the source of
truth; the layer projects a windowed view at read time and never mutates or
duplicates it. Writes and consolidation are no-ops.
"""

from __future__ import annotations

from typing import Any

from foundry.config import WorkingMemoryLayerConfig
from foundry.core import (
    FoundryMessage,
    LayerKind,
    MemoryContext,
    MemoryContribution,
    MemoryWrite,
)
from foundry.memory._text import contribution_tokens, estimate_tokens

_CHARS_PER_TOKEN = 4


class WorkingMemoryLayer:
    kind: LayerKind = "working"

    def __init__(self, config: WorkingMemoryLayerConfig) -> None:
        self.name = config.name
        self._config = config

    async def read(self, query: str, ctx: MemoryContext) -> MemoryContribution:
        raw = ctx.state_view.get(self._config.source_field)
        if isinstance(raw, str):
            return self._read_string(raw)
        messages = _coerce_messages(raw)
        window = self._config.window
        if window.max_messages is not None:
            windowed = messages[-window.max_messages:]
        else:
            assert window.max_tokens is not None  # schema: exactly one set
            windowed = list(messages)
            while windowed and contribution_tokens(windowed) > window.max_tokens:
                windowed.pop(0)
        return MemoryContribution(
            layer_name=self.name,
            layer_kind=self.kind,
            content=windowed,
            tokens_estimate=contribution_tokens(windowed),
        )

    def _read_string(self, raw: str) -> MemoryContribution:
        max_tokens = self._config.window.max_tokens
        if max_tokens is not None and estimate_tokens(raw) > max_tokens:
            # Keep the TAIL of a string transcript — recency wins.
            raw = raw[-(max_tokens * _CHARS_PER_TOKEN):]
        return MemoryContribution(
            layer_name=self.name,
            layer_kind=self.kind,
            content=raw,
            tokens_estimate=estimate_tokens(raw),
        )

    async def write(self, content: MemoryWrite, ctx: MemoryContext) -> None:
        """No-op: working memory is read-only against state (invariant 5).
        The conversation mutates via the state field's reducer."""

    async def consolidate(self, ctx: MemoryContext) -> None:
        """No-op: nothing to consolidate in a pure projection."""


def _coerce_messages(raw: Any) -> list[FoundryMessage]:
    if raw is None:
        return []
    out: list[FoundryMessage] = []
    for item in raw:
        if isinstance(item, FoundryMessage):
            out.append(item)
        else:
            out.append(FoundryMessage.model_validate(item))
    return out


__all__ = ["WorkingMemoryLayer"]
