"""Schemas for utc_now@v1."""

from pydantic import BaseModel, ConfigDict


class UtcNowIn(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UtcNowOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    iso_time: str
    unix_ms: int
