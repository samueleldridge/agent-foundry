"""Config schema for postgres@v1."""

from pydantic import BaseModel, ConfigDict, Field


class PostgresConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    port: int = Field(default=5432, ge=1, le=65535)
    database: str
    ssl_mode: str = "prefer"
    connect_timeout_s: float = Field(default=10.0, gt=0)
    min_pool_size: int = Field(default=1, ge=0)
    max_pool_size: int = Field(default=4, ge=1)
