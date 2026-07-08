"""Schemas for word_count@v1."""

from pydantic import BaseModel, ConfigDict, Field


class WordCountIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=0, description="Text to analyse.")


class WordCountOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    words: int
    characters: int
    lines: int
