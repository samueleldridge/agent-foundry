"""Schemas for http_get_json@v1."""

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class HttpGetIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(
        default="/",
        pattern=r"^/",
        description="Path relative to the connection's base_url; must start with '/'.",
    )
    query: dict[str, str] = Field(default_factory=dict)


class HttpGetOut(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status_code: int
    json_body: Any = None
    elapsed_ms: int
