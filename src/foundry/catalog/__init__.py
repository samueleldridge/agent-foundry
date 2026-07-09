"""Catalog: index + version discovery for shared tools, connections, and
retrievers — plus human-gated promotion (`foundry catalog promote`,
Phase 5). Reads are Phase 2a; `promote_artifact` is the ONLY write path
into the catalog (docs/50 invariant 6).
"""

from __future__ import annotations

from foundry.catalog.loader import (
    LoadedConnectionVersion,
    LoadedRetrieverVersion,
    LoadedToolVersion,
    catalog_entries,
    load_catalog_index,
    load_connection_contract,
    load_connection_version,
    load_retriever_version,
    load_tool_contract,
    load_tool_version,
    load_versions_metadata,
)
from foundry.catalog.promote import PromotionResult, promote_artifact
from foundry.catalog.schemas import (
    CatalogEntry,
    CatalogIndex,
    VersionMetadata,
    VersionsMetadata,
)

__all__ = [
    "CatalogEntry",
    "CatalogIndex",
    "LoadedConnectionVersion",
    "LoadedRetrieverVersion",
    "LoadedToolVersion",
    "PromotionResult",
    "VersionMetadata",
    "VersionsMetadata",
    "catalog_entries",
    "load_catalog_index",
    "load_connection_contract",
    "load_connection_version",
    "load_retriever_version",
    "load_tool_contract",
    "load_tool_version",
    "load_versions_metadata",
    "promote_artifact",
]
