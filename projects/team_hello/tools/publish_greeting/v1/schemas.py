"""Schemas for publish_greeting@v1."""

from pydantic import BaseModel, ConfigDict


class PublishIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str


class PublishOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    published: bool
    detail: str
