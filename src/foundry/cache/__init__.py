"""Concrete cache layer (docs/24): semantic + tool-result backends, key
construction, and the runtime lookup/store flows.

Import boundaries: consumers get caches ONLY via ``Session.cache`` — this
package is wired by the runtime adapter at compile/run time.
"""

from __future__ import annotations

from foundry.cache.keys import (
    agent_version_hash,
    build_semantic_cache_key,
    concat_text_content,
    cosine_similarity,
    messages_structural_hash,
    model_binding_hash,
    stable_hash,
    tools_hash,
)
from foundry.cache.runtime import (
    PreparedSemanticCache,
    default_result_cache_path,
    ensure_version_marker,
    prepare_semantic_cache,
    semantic_lookup,
    semantic_store,
)
from foundry.cache.semantic import (
    InProcessSemanticCache,
    PgVectorSemanticCache,
    RedisSemanticCache,
    VersionMarkedSemanticCache,
)
from foundry.cache.tool_result import (
    InProcessResultCache,
    PostgresResultCache,
    RedisResultCache,
)

__all__ = [
    "InProcessResultCache",
    "InProcessSemanticCache",
    "PgVectorSemanticCache",
    "PostgresResultCache",
    "PreparedSemanticCache",
    "RedisResultCache",
    "RedisSemanticCache",
    "VersionMarkedSemanticCache",
    "agent_version_hash",
    "build_semantic_cache_key",
    "concat_text_content",
    "cosine_similarity",
    "default_result_cache_path",
    "ensure_version_marker",
    "messages_structural_hash",
    "model_binding_hash",
    "prepare_semantic_cache",
    "semantic_lookup",
    "semantic_store",
    "stable_hash",
    "tools_hash",
]
