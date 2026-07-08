"""Output schema for rag_agent."""

from pydantic import BaseModel, Field


class Answer(BaseModel):
    answer: str
    sources: list[str] = Field(default_factory=list)
    """Ids of the retrieved documents the answer draws on."""
