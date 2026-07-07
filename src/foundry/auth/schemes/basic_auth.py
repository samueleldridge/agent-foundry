"""basic_auth scheme: classic ``Authorization: Basic base64(user:pass)``."""

from __future__ import annotations

import base64

from pydantic import BaseModel, ConfigDict

from foundry.core.connection import ResolvedConnectionCredentials


class BasicAuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    header_name: str = "Authorization"
    username_credential: str = "username"
    password_credential: str = "password"


def build_headers(
    config: BasicAuthConfig, credentials: ResolvedConnectionCredentials
) -> dict[str, str]:
    user = credentials.require(config.username_credential).reveal()
    password = credentials.require(config.password_credential).reveal()
    encoded = base64.b64encode(f"{user}:{password}".encode()).decode("ascii")
    return {config.header_name: f"Basic {encoded}"}


__all__ = ["BasicAuthConfig", "build_headers"]
