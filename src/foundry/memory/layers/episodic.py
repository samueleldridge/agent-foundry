"""Episodic memory: retrieval over past episodes (docs/26 § Episodic).

Wraps an existing ``Retriever`` bound through the agent's retriever slots —
there is no separate episodic store primitive (invariant 6). Read is a
retrieval call; write is best-effort ingestion when the underlying retriever
exposes an ``ingest`` method (in-process corpora do; read-only stores don't).
"""

from __future__ import annotations

import inspect

from foundry.config import EpisodicMemoryLayerConfig
from foundry.core import (
    LayerKind,
    MemoryContext,
    MemoryContribution,
    MemoryWrite,
    MemoryWriteEvent,
    Retriever,
)
from foundry.core.tool import EmitFn
from foundry.memory._text import contribution_tokens, message_text


class EpisodicMemoryLayer:
    kind: LayerKind = "episodic"

    def __init__(
        self,
        config: EpisodicMemoryLayerConfig,
        retriever: Retriever,
        *,
        emit: EmitFn | None = None,
        agent_name: str = "",
    ) -> None:
        self.name = config.name
        self._config = config
        self._retriever = retriever
        self._emit = emit
        self._agent_name = agent_name

    async def read(self, query: str, ctx: MemoryContext) -> MemoryContribution:
        docs = await self._retriever.retrieve(query, top_k=self._config.top_k)
        kept = [
            doc for doc in docs if doc.score >= self._config.relevance_threshold
        ][: self._config.top_k]
        return MemoryContribution(
            layer_name=self.name,
            layer_kind=self.kind,
            content=kept,
            tokens_estimate=contribution_tokens(kept),
            metadata={"retriever": getattr(self._retriever, "name", "")},
        )

    async def write(self, content: MemoryWrite, ctx: MemoryContext) -> None:
        """Ingest a completed message into the episode corpus when the
        underlying retriever supports it; silently skip otherwise (a
        read-only corpus is a valid episodic source)."""
        if content.kind != "message":
            return
        target = getattr(self._retriever, "retriever", self._retriever)
        ingest = getattr(target, "ingest", None)
        if ingest is None:
            return
        text = (
            content.content
            if isinstance(content.content, str)
            else message_text(content.content)
        )
        if not text:
            return
        result = ingest([text])
        if inspect.isawaitable(result):
            await result
        if self._emit is not None:
            self._emit(
                MemoryWriteEvent,
                agent_name=self._agent_name,
                layer_name=self.name,
                layer_kind=self.kind,
                write_kind=content.kind,
                bytes=len(text.encode()),
            )

    async def consolidate(self, ctx: MemoryContext) -> None:
        """No-op: episodic memory has no synthesis step."""


__all__ = ["EpisodicMemoryLayer"]
