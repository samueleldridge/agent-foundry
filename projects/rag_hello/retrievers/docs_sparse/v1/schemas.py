"""Config schema for docs_sparse@v1."""

from pydantic import BaseModel, ConfigDict


class DocsSparseConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corpus_path: str
    """JSON corpus file, relative to the project directory."""
