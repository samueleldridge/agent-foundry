"""Semantic memory: synthesised content in a state field, refreshed by an
LLM consolidator on configured triggers (docs/26 § Semantic).

Phase 2c scope: the consolidator runs on the AGENT'S model binding (the same
resolved provider adapter the agent step uses); a separate
``consolidator_model_binding`` override is deferred (see the phase handoff).
"""

from __future__ import annotations

import time
from typing import Any, Protocol, runtime_checkable

from foundry.config import SemanticMemoryLayerConfig
from foundry.core import (
    FoundryMessage,
    LayerKind,
    MemoryConsolidate,
    MemoryContext,
    MemoryContribution,
    MemoryWrite,
    MessageRole,
    ModelResponse,
    TextBlock,
)
from foundry.core.errors import MemoryConsolidateError
from foundry.core.tool import EmitFn
from foundry.memory._text import estimate_tokens, render_messages


@runtime_checkable
class SupportsGenerate(Protocol):
    """The slice of a provider adapter the consolidator needs."""

    async def generate(
        self,
        messages: list[FoundryMessage],
        tools: list[Any],
        settings: Any = None,
        session: Any = None,
    ) -> ModelResponse: ...


class SemanticMemoryLayer:
    kind: LayerKind = "semantic"

    def __init__(
        self,
        config: SemanticMemoryLayerConfig,
        *,
        consolidator_prompt_text: str | None = None,
        provider: SupportsGenerate | None = None,
        emit: EmitFn | None = None,
        agent_name: str = "",
    ) -> None:
        self.name = config.name
        self._config = config
        self._prompt_text = consolidator_prompt_text
        self._provider = provider
        self._emit = emit
        self._agent_name = agent_name

    async def read(self, query: str, ctx: MemoryContext) -> MemoryContribution:
        content = ctx.state_view.get(self._config.state_field) or ""
        text = content if isinstance(content, str) else str(content)
        return MemoryContribution(
            layer_name=self.name,
            layer_kind=self.kind,
            content=text,
            tokens_estimate=estimate_tokens(text),
        )

    async def write(self, content: MemoryWrite, ctx: MemoryContext) -> None:
        """Direct writes land only when explicitly targeted at this layer
        (e.g. a future `remember` tool); the normal update path is
        consolidation."""
        if content.target_layer != self.name or ctx.state_writer is None:
            return
        text = (
            content.content
            if isinstance(content.content, str)
            else render_messages([content.content])
        )
        ctx.state_writer(self._config.state_field, text)

    def consolidation_due(self, turn_count: int) -> bool:
        every_n = self._config.consolidate_every_n_turns
        return every_n is not None and turn_count > 0 and turn_count % every_n == 0

    async def consolidate(self, ctx: MemoryContext) -> None:
        """Run the consolidator prompt and write the synthesis back to the
        state field. Failures raise MemoryConsolidateError; the coordinator
        decides fail-open vs strict (docs/26 § Failure modes)."""
        if self._prompt_text is None or self._provider is None:
            return
        if ctx.state_writer is None:
            raise MemoryConsolidateError(
                f"semantic layer {self.name!r} cannot consolidate: no state "
                "writer available (agent lacks write access?)",
                context={"layer": self.name,
                         "state_field": self._config.state_field},
            )
        current = str(ctx.state_view.get(self._config.state_field) or "(empty)")
        recent = render_messages(ctx.recent_messages) or "(none)"
        prompt = (
            self._prompt_text
            .replace("{current}", current)
            .replace("{recent_messages}", recent)
            .replace("{max_size_tokens}", str(self._config.max_size_tokens))
        )
        started = time.monotonic()
        try:
            response = await self._provider.generate(
                [
                    FoundryMessage(
                        role=MessageRole.USER,
                        content=[TextBlock(text=prompt)],
                    )
                ],
                [],
                None,
                ctx.session,
            )
        except Exception as exc:
            raise MemoryConsolidateError(
                f"semantic layer {self.name!r} consolidator call failed: {exc}",
                context={"layer": self.name,
                         "state_field": self._config.state_field},
                cause=exc if isinstance(exc, Exception) else None,
            ) from exc
        synthesis = "".join(
            block.text
            for block in response.message.content
            if isinstance(block, TextBlock)
        ).strip()
        if not synthesis:
            raise MemoryConsolidateError(
                f"semantic layer {self.name!r} consolidator returned no text; "
                "prior synthesis preserved",
                context={"layer": self.name},
            )
        # Transactional pairing (docs/26): the state write happens together
        # with the consolidate event emission.
        ctx.state_writer(self._config.state_field, synthesis)
        if self._emit is not None:
            self._emit(
                MemoryConsolidate,
                agent_name=self._agent_name,
                layer_name=self.name,
                trigger="periodic",
                input_tokens_summarised=response.usage.input_tokens,
                output_tokens_written=response.usage.output_tokens,
                latency_ms=int((time.monotonic() - started) * 1000),
            )

    @property
    def state_field(self) -> str:
        return self._config.state_field


__all__ = ["SemanticMemoryLayer", "SupportsGenerate"]
