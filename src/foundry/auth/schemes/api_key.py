"""api_key scheme: static key injected into a header (docs/23 § api_key).

Covers ~60% of SaaS APIs. The key comes from credentials; the header name
and value format are config. No refresh.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from foundry.core.connection import ResolvedConnectionCredentials


class APIKeyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    header_name: str = "Authorization"
    value_template: str = "Bearer {api_key}"
    key_credential: str = "api_key"
    """Field name in ResolvedConnectionCredentials.fields holding the key."""


def build_headers(
    config: APIKeyConfig, credentials: ResolvedConnectionCredentials
) -> dict[str, str]:
    key = credentials.require(config.key_credential).reveal()
    return {config.header_name: config.value_template.format(**{config.key_credential: key})}


__all__ = ["APIKeyConfig", "build_headers"]
