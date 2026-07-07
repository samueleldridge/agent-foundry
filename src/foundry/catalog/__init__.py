"""Catalog: index + version discovery for shared tools and connections.

Read-only in Phase 2a; promotion (`foundry catalog promote`) lands Phase 5.
"""

from __future__ import annotations

from foundry.catalog.loader import (
    LoadedConnectionVersion,
    LoadedToolVersion,
    catalog_entries,
    load_catalog_index,
    load_connection_version,
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
    "LoadedToolVersion",
    "VersionMetadata",
    "VersionsMetadata",
    "catalog_entries",
    "load_catalog_index",
    "load_connection_version",
    "load_tool_version",
    "load_versions_metadata",
]
