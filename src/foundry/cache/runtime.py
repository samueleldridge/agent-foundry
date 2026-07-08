"""Semantic-cache compile-time preparation + run-time lookup/store flow
(docs/24 § Layer 2), kept out of the runtime adapter so it stays thin.

Compile time (``prepare_semantic_cache``): resolve the embedder binding
(unknown provider/model → ``EmbedderConfigError``), enforce the dimension
match against the backend's configured dimensions (LOAD-time, never first
call), compute the agent-version content hash, and construct the backend.

Run time (``semantic_lookup`` / ``semantic_store``): build the key (one
embed call → ``embed`` event), enforce agent-version invalidation via the
backend's version marker, and fail OPEN on every ``EmbedderError`` /
``CacheError`` — a broken cache must never block a run (invariant 4).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import httpx

from foundry.cache.keys import agent_version_hash, build_semantic_cache_key
from foundry.cache.semantic import (
    InProcessSemanticCache,
    PgVectorSemanticCache,
    RedisSemanticCache,
    VersionMarkedSemanticCache,
)
from foundry.config import SemanticCacheConfig
from foundry.config.schemas import AgentSpec
from foundry.core import (
    EmbedCall,
    FoundryMessage,
    ModelResponse,
    SemanticCacheHitEvent,
    SemanticCacheInvalidate,
    SemanticCacheKey,
    SemanticCacheMiss,
    SemanticCacheStore,
    WarningEvent,
)
from foundry.core.errors import (
    CacheError,
    EmbedderConfigError,
    EmbedderError,
)
from foundry.core.tool import EmitFn
from foundry.providers import ModelBinding, ToolSchema
from foundry.providers._registry import SecretsResolver
from foundry.providers.embedders import (
    EmbedderAdapter,
    embedder_capabilities,
    load_embedder,
)
from foundry.storage.paths import foundry_home


def _scope_key(config: SemanticCacheConfig, project: str, agent_name: str) -> str:
    if config.scope == "agent":
        return f"agent:{project}/{agent_name}"
    if config.scope == "project":
        return f"project:{project}"
    return "global"


@dataclass(frozen=True)
class PreparedSemanticCache:
    """Everything the run needs, resolved and validated at compile time."""

    config: SemanticCacheConfig
    backend: VersionMarkedSemanticCache
    embedder: EmbedderAdapter
    agent_version: str
    """Content hash of (agent spec + prompt text) — docs/24 rule 1."""


def prepare_semantic_cache(
    agent_spec: AgentSpec,
    prompt_text: str,
    *,
    project: str,
    secrets: SecretsResolver,
    transport: httpx.AsyncBaseTransport | None = None,
) -> PreparedSemanticCache | None:
    """Compile-time preparation. Returns None when the agent has no (enabled)
    semantic_cache block. Raises EmbedderConfigError on unknown embedder or
    dimension mismatch — at LOAD, before any call."""
    config = agent_spec.semantic_cache
    if config is None or not config.enabled:
        return None

    binding = config.embedder_binding
    capabilities = embedder_capabilities(binding.provider, binding.model)

    configured_dims = config.backend_config.get("dimensions")
    if configured_dims is not None and int(configured_dims) != capabilities.dimensions:
        raise EmbedderConfigError(
            f"semantic_cache dimension mismatch for agent "
            f"{agent_spec.name!r}: embedder {binding.provider}/{binding.model} "
            f"produces {capabilities.dimensions}-dimensional vectors but the "
            f"'{config.backend}' backend is configured for {configured_dims} "
            "(backend_config.dimensions). Re-index the backend or switch "
            "embedders (docs/24 § Dimension compatibility).",
            context={
                "agent": agent_spec.name,
                "embedder": f"{binding.provider}/{binding.model}",
                "embedder_dimensions": capabilities.dimensions,
                "backend": config.backend,
                "backend_dimensions": int(configured_dims),
            },
        )

    embedder = load_embedder(binding, secrets, transport=transport)
    scope_key = _scope_key(config, project, agent_spec.name)
    backend = _build_backend(config, scope_key, capabilities.dimensions)
    return PreparedSemanticCache(
        config=config,
        backend=backend,
        embedder=embedder,
        agent_version=agent_version_hash(
            agent_spec.model_dump(mode="json", by_alias=True), prompt_text
        ),
    )


def _build_backend(
    config: SemanticCacheConfig, scope_key: str, dimensions: int
) -> VersionMarkedSemanticCache:
    if config.backend == "in_process":
        path = config.backend_config.get(
            "path", str(foundry_home() / "cache" / "semantic.db")
        )
        return InProcessSemanticCache(
            Path(path), scope_key=scope_key, max_entries=config.max_entries
        )
    if config.backend == "redis":
        return RedisSemanticCache(
            str(config.backend_config.get("url", "redis://localhost:6379/0")),
            scope_key=scope_key,
            max_entries=config.max_entries,
        )
    # pgvector
    return PgVectorSemanticCache(
        dsn=config.backend_config.get("dsn"),
        table=str(config.backend_config.get("table", "foundry_semantic_cache")),
        scope_key=scope_key,
        dimensions=int(config.backend_config.get("dimensions", dimensions)),
        max_entries=config.max_entries,
    )


async def ensure_version_marker(
    prepared: PreparedSemanticCache, agent_name: str, emit: EmitFn | None
) -> None:
    """docs/24 correctness rule 1: an agent-version change invalidates that
    agent's entries BEFORE the new version serves. Fail-open on backend
    errors (a warning, not a blocked run)."""
    try:
        previous = await prepared.backend.version_marker(agent_name)
        if previous is not None and previous != prepared.agent_version:
            await prepared.backend.invalidate(agent_name)
            if emit is not None:
                emit(
                    SemanticCacheInvalidate,
                    agent_name=agent_name,
                    reason="agent_version_changed",
                    previous_version=previous,
                    current_version=prepared.agent_version,
                )
        if previous != prepared.agent_version:
            await prepared.backend.set_version_marker(
                agent_name, prepared.agent_version
            )
    except CacheError as exc:
        _warn(emit, agent_name, "cache.semantic.error",
              f"version-marker check failed; continuing without "
              f"invalidation: {exc}", exc)


async def semantic_lookup(
    prepared: PreparedSemanticCache,
    *,
    agent_name: str,
    model_binding: ModelBinding,
    tools: list[ToolSchema],
    messages: list[FoundryMessage],
    emit: EmitFn | None,
) -> tuple[ModelResponse | None, SemanticCacheKey | None]:
    """Returns (cached response | None, key | None). The key is reused by
    ``semantic_store`` after a miss; both are None when the embed or lookup
    failed (fail-open: caller proceeds straight to the LLM)."""
    try:
        key = await build_semantic_cache_key(
            agent_name=agent_name,
            agent_version=prepared.agent_version,
            model_binding=model_binding,
            tools=tools,
            messages=messages,
            embedder=prepared.embedder,
        )
    except EmbedderError as exc:
        _warn(emit, agent_name, "cache.semantic.embedder_error",
              f"embedder unavailable; skipping semantic cache: {exc}", exc)
        return None, None
    if emit is not None:
        embedding = key.messages_embedding
        emit(
            EmbedCall,
            agent_name=agent_name,
            embedder=prepared.embedder.name,
            input_count=1,
            input_tokens=embedding.input_tokens,
            purpose="query",
            latency_ms=embedding.latency_ms,
            cost_estimate_usd=embedding.cost_estimate_usd,
        )

    threshold = prepared.config.similarity_threshold
    try:
        hit = await prepared.backend.lookup(key, threshold)
    except CacheError as exc:
        _warn(emit, agent_name, "cache.semantic.error",
              f"semantic cache lookup failed; calling the LLM "
              f"(fail-open): {exc}", exc)
        return None, key

    if hit is None:
        if emit is not None:
            emit(
                SemanticCacheMiss,
                agent_name=agent_name,
                top_similarity=float(
                    getattr(prepared.backend, "last_top_similarity", 0.0)
                ),
                threshold=threshold,
            )
        return None, key

    usage = hit.response.usage
    if emit is not None:
        emit(
            SemanticCacheHitEvent,
            agent_name=agent_name,
            similarity=hit.similarity,
            threshold=threshold,
            cached_at=hit.cached_at,
            saved_tokens_estimate=usage.input_tokens + usage.output_tokens,
            saved_cost_estimate_usd=hit.response.cost_estimate_usd
            or Decimal("0"),
        )
    return hit.response, key


async def semantic_store(
    prepared: PreparedSemanticCache,
    key: SemanticCacheKey,
    response: ModelResponse,
    *,
    agent_name: str,
    emit: EmitFn | None,
) -> None:
    try:
        await prepared.backend.store(key, response, prepared.config.ttl_s)
    except CacheError as exc:
        _warn(emit, agent_name, "cache.semantic.error",
              f"semantic cache store failed; response NOT cached "
              f"(fail-open): {exc}", exc)
        return
    if emit is not None:
        emit(
            SemanticCacheStore,
            agent_name=agent_name,
            ttl_s=prepared.config.ttl_s,
        )


def _warn(
    emit: EmitFn | None, agent_name: str, category: str, message: str,
    exc: Exception,
) -> None:
    if emit is not None:
        emit(
            WarningEvent,
            agent_name=agent_name,
            category=category,
            message=message,
            error_class=type(exc).__name__,
        )


def default_result_cache_path() -> Path:
    return foundry_home() / "cache" / "tool_results.db"


__all__ = [
    "PreparedSemanticCache",
    "default_result_cache_path",
    "ensure_version_marker",
    "prepare_semantic_cache",
    "semantic_lookup",
    "semantic_store",
]
