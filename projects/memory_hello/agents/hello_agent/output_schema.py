"""Output schema for hello_agent."""

from pydantic import BaseModel


class Reply(BaseModel):
    reply: str
