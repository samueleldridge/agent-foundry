"""Cache protocols + key/hit types (docs/10 § Caching primitives, docs/24).

Phase 2b: concrete backends live in ``foundry.cache``; this module owns the
shapes shared across layers — ``SemanticCacheKey`` / ``SemanticCacheHit``,
the ``SemanticCache`` / ``ResultCache`` protocols, the ``CacheBundle`` that
rides on ``Session``, and the scope-key convention for tool-result caching.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from foundry.core.embedder import Embedding
from foundry.core.model import ModelResponse

CacheScope = Literal["agent", "project", "global"]


class SemanticCacheKey(BaseModel):
    model_config = ConfigDict(frozen=True)

    agent_name: str
    agent_version: str
    model_binding_hash: str
    tools_hash: str
    messages_structural_hash: str
    messages_embedding: Embedding

    def bucket(self) -> str:
        """The exact-match bucket similarity search is confined to
        (docs/24 § Key construction: no cross-bucket similarity hits)."""
        return (
            f"{self.agent_name}|{self.agent_version}|{self.model_binding_hash}"
            f"|{self.tools_hash}|{self.messages_structural_hash}"
        )


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


class CachedToolResult(BaseModel):
    """What a ``ResultCache`` lookup returns: the stored output payload plus
    provenance. The dispatcher re-validates ``output`` against the tool's
    output schema before returning it (a stale schema → CacheCorruptedEntry
    semantics: evict, warn, run the handler)."""

    model_config = ConfigDict(frozen=True)

    output: dict[str, Any] = Field(default_factory=dict)
    cached_at: datetime


@runtime_checkable
class ResultCache(Protocol):
    async def lookup(
        self, tool_ref: str, tool_version: str, input_hash: str
    ) -> CachedToolResult | None: ...

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


@dataclass(frozen=True)
class CacheBundle:
    """Concrete ``CacheAccessor`` carried on ``Session.cache`` (docs/10
    § CacheAccessor on Session). Either side is None when not configured."""

    semantic: SemanticCache | None = None
    tool_result: ResultCache | None = None


def scoped_input_hash(
    scope: CacheScope, project: str, agent_name: str, input_hash: str
) -> str:
    """Fold ToolSpec.cache_scope into the exact-match key so 'agent'-scoped
    entries never leak across agents and 'project' never across projects
    (docs/24 § Layer 3 configuration)."""
    if scope == "agent":
        qualifier = f"agent:{project}/{agent_name}"
    elif scope == "project":
        qualifier = f"project:{project}"
    else:
        qualifier = "global"
    return f"{qualifier}:{input_hash}"


__all__ = [
    "CacheAccessor",
    "CacheBundle",
    "CacheScope",
    "CachedToolResult",
    "ResultCache",
    "SemanticCache",
    "SemanticCacheHit",
    "SemanticCacheKey",
    "scoped_input_hash",
]
