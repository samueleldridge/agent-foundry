"""Config schema for pgvector_dense@v1."""

from pydantic import BaseModel, ConfigDict, Field

from foundry.providers.embedders import EmbedderBinding


class PgVectorDenseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    embedder_binding: EmbedderBinding
    """Embedder used for query vectors (documents were ingested with the
    same model — swapping it requires re-indexing; the dimension check
    against the connection's embedding_dimensions enforces agreement)."""
    table: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    id_column: str = "id"
    text_column: str = "text"
    embedding_column: str = "embedding"
    source_column: str | None = None
