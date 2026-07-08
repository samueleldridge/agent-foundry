"""The three standard memory layers (docs/26 § Layer kinds)."""

from __future__ import annotations

from foundry.memory.layers.episodic import EpisodicMemoryLayer
from foundry.memory.layers.semantic import SemanticMemoryLayer, SupportsGenerate
from foundry.memory.layers.working import WorkingMemoryLayer

__all__ = [
    "EpisodicMemoryLayer",
    "SemanticMemoryLayer",
    "SupportsGenerate",
    "WorkingMemoryLayer",
]
