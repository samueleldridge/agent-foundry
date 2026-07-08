"""Multi-layer memory: coordinator, standard layers, prompt assembly, wiring.

Protocols + value types live in ``foundry.core.memory``; configuration in
``foundry.config.schemas`` (MemoryConfig). See docs/26.
"""

from __future__ import annotations

from foundry.memory.coordinator import DefaultMemory
from foundry.memory.layers import (
    EpisodicMemoryLayer,
    SemanticMemoryLayer,
    SupportsGenerate,
    WorkingMemoryLayer,
)
from foundry.memory.prompt_assembly import WovenPrompt, weave
from foundry.memory.wiring import PreparedMemory, build_memory, prepare_memory

__all__ = [
    "DefaultMemory",
    "EpisodicMemoryLayer",
    "PreparedMemory",
    "SemanticMemoryLayer",
    "SupportsGenerate",
    "WorkingMemoryLayer",
    "WovenPrompt",
    "build_memory",
    "prepare_memory",
    "weave",
]
