"""Config schema for episode_store@v1."""

from pydantic import BaseModel, ConfigDict


class EpisodeStoreConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corpus_path: str
    """JSON episode file, relative to the project directory."""
