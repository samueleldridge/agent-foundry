"""Config schema for http_service@v1 (api_key auth)."""

from pydantic import BaseModel, ConfigDict, Field


class HTTPServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(pattern=r"^https?://")
    timeout_s: float = Field(default=10.0, gt=0)
    health_path: str = Field(default="/", pattern=r"^/")
    api_key_header: str = "Authorization"
    api_key_template: str = "Bearer {api_key}"
