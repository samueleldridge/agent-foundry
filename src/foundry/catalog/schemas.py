"""Catalog metadata models: CatalogIndex, CatalogEntry, VersionsMetadata.

Per docs/12 § CatalogIndex and VersionsMetadata. Phase 2a adds the
``connections`` list to CatalogIndex (docs/12's sketch predates the
connection catalog; docs/03 § Phase 2a requires the index to list both).
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CatalogIndex(BaseModel):
    """Shape of ``catalog/index.yaml`` (one per catalog root)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    tools: list[str] = Field(default_factory=list)
    """Tool directory names under <root>/tools/."""
    connections: list[str] = Field(default_factory=list)
    """Connection directory names under <root>/connections/."""
    retrievers: list[str] = Field(default_factory=list)
    """Retriever (and reranker) directory names under <root>/retrievers/."""
    agent_templates: list[str] = Field(default_factory=list)
    """Optional feature; unused until agent templates land."""


class VersionMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: str = Field(pattern=r"^v\d+$")
    created_at: datetime
    created_by: Literal["human", "meta_agent"]
    eval_score: float | None = None
    eval_run_id: str | None = None
    notes: str = ""
    deprecated: bool = False
    deprecation_reason: str | None = None


class VersionsMetadata(BaseModel):
    """Shape of ``<artifact>/versions.json`` (catalog or project-local)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    versions: list[VersionMetadata]

    def get(self, version: str) -> VersionMetadata | None:
        for entry in self.versions:
            if entry.version == version:
                return entry
        return None


class CatalogEntry(BaseModel):
    """One artifact as listed by the catalog (name + versions on disk)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["tool", "connection", "retriever"]
    versions: list[str]
    latest: str | None = None
    """Contents of the LATEST pointer file, when present."""
    root: str
    """Catalog root this entry was discovered under."""


__all__ = [
    "CatalogEntry",
    "CatalogIndex",
    "VersionMetadata",
    "VersionsMetadata",
]
