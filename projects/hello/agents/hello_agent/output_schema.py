"""Output schema for hello_agent."""

from pydantic import BaseModel


class Greeting(BaseModel):
    greeting: str
