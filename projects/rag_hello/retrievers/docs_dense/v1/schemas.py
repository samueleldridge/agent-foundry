"""Config schema for docs_dense@v1."""

from pydantic import BaseModel, ConfigDict, Field

from foundry.providers.embedders import EmbedderBinding


class DocsDenseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corpus_path: str
    """JSON corpus file, relative to the project directory."""
    dimensions: int = Field(ge=1, le=8192)
    """The index's vector dimensionality — checked against the embedder at
    LOAD (mismatch -> EmbedderConfigError before anything runs)."""
    embedder_binding: EmbedderBinding
