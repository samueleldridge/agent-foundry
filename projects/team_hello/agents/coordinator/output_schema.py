"""Output schema for the coordinator — the system's final answer."""

from pydantic import BaseModel


class TeamReport(BaseModel):
    final_summary: str
