"""Phase 2b schema surface: tool cache fields, semantic-cache config,
retriever bindings, and the extended ArtifactRef kinds."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from foundry.config import (
    AgentSpec,
    ArtifactRef,
    EmbedderBinding,
    FoundryRoots,
    RetrieverBinding,
    RetrieverSpec,
    SemanticCacheConfig,
    ToolSpec,
)
from foundry.core.errors import RefResolutionError

_TOOL_BASE: dict[str, Any] = {
    "name": "echo",
    "version": "v1",
    "description": "echoes",
    "input_schema": "schemas.py::In",
    "output_schema": "schemas.py::Out",
    "handler": "handler.py::handle",
}

_AGENT_BASE: dict[str, Any] = {
    "name": "agent_a",
    "model_binding": {"provider": "anthropic", "model": "claude-haiku-4-5"},
    "prompt": {"version": "v1", "path": "prompts/v1.md"},
    "output": {"schema": "output_schema.py::Out"},
    "state_visibility": {"read": ["q"], "write": ["a"]},
}


# --- ToolSpec cache validator (docs/24: both set or both unset) ------------------


@pytest.mark.unit
def test_cacheable_without_ttl_rejected_at_validation() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ToolSpec.model_validate({**_TOOL_BASE, "cacheable": True})
    assert "cache_ttl_s" in str(excinfo.value)


@pytest.mark.unit
def test_ttl_without_cacheable_rejected() -> None:
    with pytest.raises(ValidationError) as excinfo:
        ToolSpec.model_validate({**_TOOL_BASE, "cache_ttl_s": 60})
    assert "cacheable" in str(excinfo.value)


@pytest.mark.unit
def test_cacheable_with_ttl_accepted_with_project_default_scope() -> None:
    spec = ToolSpec.model_validate(
        {**_TOOL_BASE, "cacheable": True, "cache_ttl_s": 300}
    )
    assert spec.cache_scope == "project"


# --- SemanticCacheConfig -----------------------------------------------------------


@pytest.mark.unit
def test_semantic_cache_threshold_clamped_to_half_to_one() -> None:
    base = {"embedder_binding": {"provider": "voyage", "model": "voyage-3"}}
    with pytest.raises(ValidationError):
        SemanticCacheConfig.model_validate(
            {**base, "similarity_threshold": 0.4}
        )
    config = SemanticCacheConfig.model_validate(base)
    assert config.similarity_threshold == 0.95  # docs/24 default
    assert config.backend == "in_process" and config.scope == "agent"
    assert isinstance(config.embedder_binding, EmbedderBinding)


@pytest.mark.unit
def test_agent_spec_semantic_cache_off_by_default() -> None:
    spec = AgentSpec.model_validate(_AGENT_BASE)
    assert spec.semantic_cache is None  # docs/24 invariant 1
    assert spec.retrievers == []


# --- RetrieverBinding on AgentSpec ---------------------------------------------------


@pytest.mark.unit
def test_duplicate_retriever_slots_rejected() -> None:
    binding = {"slot": "kb", "ref": "catalog/hybrid_rrf", "version": "v1"}
    with pytest.raises(ValidationError) as excinfo:
        AgentSpec.model_validate({**_AGENT_BASE, "retrievers": [binding, binding]})
    assert "unique" in str(excinfo.value)


@pytest.mark.unit
def test_retriever_binding_shape_with_reranker() -> None:
    binding = RetrieverBinding.model_validate({
        "slot": "kb",
        "ref": "catalog/hybrid_rrf",
        "version": "v1",
        "top_k": 50,
        "config": {"rrf_k": 42},
        "reranker": {
            "ref": "catalog/cohere_rerank",
            "version": "v1",
            "connection_bindings": {"cohere": "cohere_api"},
            "top_k": 8,
        },
    })
    assert binding.reranker is not None and binding.reranker.top_k == 8


@pytest.mark.unit
def test_retriever_spec_accepts_reranker_kind() -> None:
    spec = RetrieverSpec.model_validate({
        "name": "cohere_rerank",
        "version": "v1",
        "description": "stage",
        "kind": "reranker",
        "config_schema": "schemas.py::Config",
        "factory": "factory.py::build_reranker",
    })
    assert spec.kind == "reranker"


# --- ArtifactRef kinds (retriever + agent_template, same code path) ------------------


@pytest.mark.unit
def test_retriever_and_agent_template_refs_resolve_through_same_path(
    tmp_path: Path,
) -> None:
    catalog = tmp_path / "catalog"
    for kind_dir in ("retrievers", "agent_templates"):
        (catalog / kind_dir / "thing" / "v1").mkdir(parents=True)
    roots = FoundryRoots(catalog_roots=[catalog], projects_root=tmp_path)

    retriever_ref = ArtifactRef.parse("catalog/thing@v1", "retriever")
    template_ref = ArtifactRef.parse("catalog/thing@v1", "agent_template")
    assert retriever_ref.resolve_path(roots) == catalog / "retrievers" / "thing" / "v1"
    assert (
        template_ref.resolve_path(roots)
        == catalog / "agent_templates" / "thing" / "v1"
    )

    # missing version → the same structured error listing available versions
    with pytest.raises(RefResolutionError) as excinfo:
        ArtifactRef.parse("catalog/thing@v9", "retriever").resolve_path(roots)
    assert excinfo.value.context["available_versions"] == ["v1"]
