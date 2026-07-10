"""Output schema for the publisher."""

from pydantic import BaseModel


class PublishOutcome(BaseModel):
    publish_status: str
