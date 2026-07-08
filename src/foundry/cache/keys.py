"""Stable hashing + SemanticCacheKey construction (docs/24 § Key
construction, normative).

The structural pieces (model-binding hash, tools hash, message-structure
hash) are exact-match: similarity is only ever evaluated INSIDE the bucket
they define. The embedding is the only fuzzy component.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from foundry.core import (
    Embedder,
    FoundryMessage,
    SemanticCacheKey,
    TextBlock,
)
from foundry.providers import ModelBinding, ToolSchema


def stable_hash(obj: Any) -> str:
    """Deterministic content hash of any JSON-serialisable structure."""
    payload = json.dumps(obj, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:32]


def concat_text_content(messages: list[FoundryMessage]) -> str:
    """The textual content the semantic similarity is computed over."""
    parts: list[str] = []
    for message in messages:
        for block in message.content:
            if isinstance(block, TextBlock):
                parts.append(block.text)
    return "\n".join(parts)


def model_binding_hash(binding: ModelBinding) -> str:
    """Exact separation across model settings (docs/24 correctness rule 2)."""
    settings = binding.settings
    return stable_hash(
        {
            "provider": binding.provider,
            "model": binding.model,
            "temperature": settings.temperature,
            "max_tokens": settings.max_tokens,
            "top_p": settings.top_p,
            "response_format": (
                settings.response_format.model_dump(mode="json")
                if settings.response_format is not None
                else None
            ),
        }
    )


def tools_hash(tools: list[ToolSchema]) -> str:
    """Exact hash of the tool schemas presented to the LLM (rule 3)."""
    return stable_hash(
        [t.model_dump(mode="json") for t in sorted(tools, key=lambda t: t.name)]
    )


def messages_structural_hash(messages: list[FoundryMessage]) -> str:
    """Structure only — role + block types, content text stripped. Catches a
    new tool-use block in history independent of semantic content."""
    return stable_hash(
        [
            {
                "role": m.role.value,
                "block_types": [getattr(b, "type", "?") for b in m.content],
            }
            for m in messages
        ]
    )


def agent_version_hash(agent_spec_dump: dict[str, Any], prompt_text: str) -> str:
    """Content-hash of the agent config at compile time. ANY prompt, tool-
    binding, or model-binding edit changes it → docs/24 correctness rule 1
    invalidates that agent's cached entries."""
    return stable_hash({"spec": agent_spec_dump, "prompt": prompt_text})


async def build_semantic_cache_key(
    *,
    agent_name: str,
    agent_version: str,
    model_binding: ModelBinding,
    tools: list[ToolSchema],
    messages: list[FoundryMessage],
    embedder: Embedder,
) -> SemanticCacheKey:
    """docs/24 § Key construction. Both stored and lookup keys embed with
    purpose='query' — they are both 'what is the agent being asked?'."""
    embeddings = await embedder.embed([concat_text_content(messages)], "query")
    return SemanticCacheKey(
        agent_name=agent_name,
        agent_version=agent_version,
        model_binding_hash=model_binding_hash(model_binding),
        tools_hash=tools_hash(tools),
        messages_structural_hash=messages_structural_hash(messages),
        messages_embedding=embeddings[0],
    )


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Plain-Python cosine — the in-process backends' vector math."""
    if len(a) != len(b) or not a:
        return -1.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return -1.0
    return dot / (norm_a * norm_b)


__all__ = [
    "agent_version_hash",
    "build_semantic_cache_key",
    "concat_text_content",
    "cosine_similarity",
    "messages_structural_hash",
    "model_binding_hash",
    "stable_hash",
    "tools_hash",
]
