"""DefaultMemory: the layer coordinator (docs/26 § Mental model).

Reads every layer in declared order, degrades failed layers to empty
contributions (or raises ``MemoryLayerError`` under ``fail_strict``), applies
the envelope token cap by truncating last-listed layers first, and emits the
``memory.read`` audit event. Writes and consolidation fan out to the layers
with the same fail-open/strict split.
"""

from __future__ import annotations

from foundry.config import MemoryConfig
from foundry.core import (
    MemoryContext,
    MemoryContribution,
    MemoryEnvelope,
    MemoryLayer,
    MemoryRead,
    MemoryWrite,
    WarningEvent,
)
from foundry.core.errors import MemoryConsolidateError, MemoryLayerError
from foundry.core.tool import EmitFn
from foundry.memory._text import truncate_contribution
from foundry.memory.layers import SemanticMemoryLayer


def _empty_contribution(layer: MemoryLayer) -> MemoryContribution:
    return MemoryContribution(
        layer_name=layer.name,
        layer_kind=layer.kind,
        content="" if layer.kind in ("semantic", "custom") else [],
        tokens_estimate=0,
    )


class DefaultMemory:
    """Concrete ``Memory`` protocol implementation."""

    def __init__(
        self,
        layers: list[MemoryLayer],
        *,
        config: MemoryConfig,
        emit: EmitFn | None = None,
        agent_name: str = "",
    ) -> None:
        self.layers = layers
        self.config = config
        self._emit = emit
        self._agent_name = agent_name

    # --- read ------------------------------------------------------------

    async def read(self, query: str, ctx: MemoryContext) -> MemoryEnvelope:
        contributions: list[MemoryContribution] = []
        failed: list[str] = []
        for layer in self.layers:
            try:
                contributions.append(await layer.read(query, ctx))
            except Exception as exc:
                if self.config.fail_strict:
                    raise MemoryLayerError(
                        f"memory layer {layer.name!r} ({layer.kind}) failed "
                        f"during read and fail_strict is set: {exc}",
                        context={"layer": layer.name, "kind": layer.kind,
                                 "agent": self._agent_name},
                        cause=exc if isinstance(exc, Exception) else None,
                    ) from exc
                failed.append(layer.name)
                self._warn(
                    "memory.layer_failed",
                    f"memory layer {layer.name!r} ({layer.kind}) failed; "
                    f"contributing nothing this turn (fail-open): {exc}",
                    error_class=type(exc).__name__,
                )
                contributions.append(_empty_contribution(layer))

        contributions, truncated_layers = self._apply_envelope_cap(contributions)
        total = sum(c.tokens_estimate for c in contributions)
        envelope = MemoryEnvelope(
            contributions=contributions,
            total_tokens_estimate=total,
            truncated=bool(truncated_layers),
            layers_truncated=truncated_layers,
            layers_failed=failed,
        )
        if self._emit is not None:
            self._emit(
                MemoryRead,
                agent_name=self._agent_name,
                layers_read=[
                    layer.name for layer in self.layers
                    if layer.name not in failed
                ],
                layers_failed=failed,
                total_tokens_estimate=total,
                truncated=envelope.truncated,
                layers_truncated=truncated_layers,
            )
        return envelope

    def _apply_envelope_cap(
        self, contributions: list[MemoryContribution]
    ) -> tuple[list[MemoryContribution], list[str]]:
        cap = self.config.max_envelope_tokens
        if cap is None:
            return contributions, []
        total = sum(c.tokens_estimate for c in contributions)
        if total <= cap:
            return contributions, []
        out = list(contributions)
        truncated: list[str] = []
        # Last-listed layer truncates first (docs/26 § Prompt assembly 6).
        for index in range(len(out) - 1, -1, -1):
            excess = total - cap
            if excess <= 0:
                break
            contribution = out[index]
            if contribution.tokens_estimate == 0:
                continue
            allowed = max(0, contribution.tokens_estimate - excess)
            new_contribution = truncate_contribution(contribution, allowed)
            total -= contribution.tokens_estimate - new_contribution.tokens_estimate
            out[index] = new_contribution
            truncated.append(contribution.layer_name)
        return out, truncated

    # --- write -----------------------------------------------------------

    async def write(self, content: MemoryWrite, ctx: MemoryContext) -> None:
        for layer in self.layers:
            if content.target_layer is not None and layer.name != content.target_layer:
                continue
            try:
                await layer.write(content, ctx)
            except Exception as exc:
                if self.config.fail_strict:
                    raise MemoryLayerError(
                        f"memory layer {layer.name!r} ({layer.kind}) failed "
                        f"during write and fail_strict is set: {exc}",
                        context={"layer": layer.name, "kind": layer.kind},
                        cause=exc if isinstance(exc, Exception) else None,
                    ) from exc
                self._warn(
                    "memory.write_failed",
                    f"memory layer {layer.name!r} write failed; the run "
                    f"continues without it (fail-open): {exc}",
                    error_class=type(exc).__name__,
                )

    # --- consolidate -------------------------------------------------------

    def consolidation_due(self, turn_count: int) -> bool:
        return any(
            isinstance(layer, SemanticMemoryLayer)
            and layer.consolidation_due(turn_count)
            for layer in self.layers
        )

    async def consolidate(self, ctx: MemoryContext) -> None:
        """Consolidate every layer that is due at this turn. Consolidator
        failures preserve the prior synthesis and warn (docs/26 invariant 4)
        unless fail_strict."""
        for layer in self.layers:
            if isinstance(layer, SemanticMemoryLayer) and not layer.consolidation_due(
                ctx.turn_count
            ):
                continue
            try:
                await layer.consolidate(ctx)
            except MemoryConsolidateError as exc:
                if self.config.fail_strict:
                    raise
                self._warn(
                    "memory.consolidate_failed",
                    f"memory layer {layer.name!r} consolidation failed; "
                    f"existing synthesis preserved (fail-open): {exc}",
                    error_class=type(exc).__name__,
                )

    def _warn(self, category: str, message: str, *, error_class: str) -> None:
        if self._emit is not None:
            self._emit(
                WarningEvent,
                agent_name=self._agent_name,
                category=category,
                message=message,
                error_class=error_class,
            )


__all__ = ["DefaultMemory"]
