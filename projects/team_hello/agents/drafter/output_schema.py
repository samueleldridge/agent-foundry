"""Output schema for the drafter."""

from pydantic import BaseModel


class Draft(BaseModel):
    draft: str
