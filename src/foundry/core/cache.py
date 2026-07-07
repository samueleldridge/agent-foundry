"""Cache protocols — Phase 1 type stubs.

Concrete semantic + result caches land in Phase 2b. Phase 1 ships the
protocol shapes so config schemas and the public ``core`` re-export are stable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from foundry.core.embedder import Embedding
from foundry.core.model import ModelResponse


class SemanticCacheKey(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_name: str
    agent_version: str
    model_binding_hash: str
    tools_hash: str
    messages_structural_hash: str
    messages_embedding: Embedding


class SemanticCacheHit(BaseModel):
    model_config = ConfigDict(frozen=True)

    response: ModelResponse
    similarity: float
    cached_at: datetime
    original_input_preview: str | None = None


@runtime_checkable
class SemanticCache(Protocol):
    async def lookup(
        self, key: SemanticCacheKey, threshold: float
    ) -> SemanticCacheHit | None: ...

    async def store(
        self, key: SemanticCacheKey, response: ModelResponse, ttl_s: int
    ) -> None: ...

    async def invalidate(self, agent_name: str) -> None: ...


@runtime_checkable
class ResultCache(Protocol):
    async def lookup(
        self, tool_ref: str, tool_version: str, input_hash: str
    ) -> BaseModel | None: ...

    async def store(
        self,
        tool_ref: str,
        tool_version: str,
        input_hash: str,
        output: BaseModel,
        ttl_s: int,
    ) -> None: ...


@runtime_checkable
class CacheAccessor(Protocol):
    semantic: SemanticCache | None
    tool_result: ResultCache | None


__all__ = [
    "CacheAccessor",
    "ResultCache",
    "SemanticCache",
    "SemanticCacheHit",
    "SemanticCacheKey",
]
