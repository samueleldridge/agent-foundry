"""Catalog: index + version discovery for shared tools, connections, and
retrievers.

Read-only in Phase 2a; promotion (`foundry catalog promote`) lands Phase 5.
"""

from __future__ import annotations

from foundry.catalog.loader import (
    LoadedConnectionVersion,
    LoadedRetrieverVersion,
    LoadedToolVersion,
    catalog_entries,
    load_catalog_index,
    load_connection_version,
    load_retriever_version,
    load_tool_version,
    load_versions_metadata,
)
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
    "VersionMetadata",
    "VersionsMetadata",
    "catalog_entries",
    "load_catalog_index",
    "load_connection_version",
    "load_retriever_version",
    "load_tool_version",
    "load_versions_metadata",
]
