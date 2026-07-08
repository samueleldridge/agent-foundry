"""Embedder adapter family (docs/11 § Embedders, docs/24 § Embedder
abstraction).

Concrete adapters are imported here so the registry is populated at startup:
Voyage + OpenAI + Cohere are functional over direct httpx; Bedrock is a
registered stub (SigV4 signing deferred — see its module docstring).
"""

from __future__ import annotations

from foundry.providers.embedders._base import (
    EmbedderAdapter,
    ParsedEmbedBatch,
    Purpose,
)
from foundry.providers.embedders._registry import (
    available_embedders,
    embedder_capabilities,
    load_embedder,
    register_embedder,
)
from foundry.providers.embedders._types import EmbedderBinding, EmbedderSettings
from foundry.providers.embedders.bedrock import BedrockEmbedder
from foundry.providers.embedders.cohere import CohereEmbedder
from foundry.providers.embedders.openai import OpenAIEmbedder
from foundry.providers.embedders.voyage import VoyageEmbedder

__all__ = [
    "BedrockEmbedder",
    "CohereEmbedder",
    "EmbedderAdapter",
    "EmbedderBinding",
    "EmbedderSettings",
    "OpenAIEmbedder",
    "ParsedEmbedBatch",
    "Purpose",
    "VoyageEmbedder",
    "available_embedders",
    "embedder_capabilities",
    "load_embedder",
    "register_embedder",
]
