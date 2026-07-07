"""Connection protocols — Phase 1 type stubs.

Full implementation lands in Phase 2a (foundry.connections + foundry.auth).
Phase 1 ships the protocol shapes so config schemas (ConnectionSpec /
ConnectionBinding) and the public ``core`` re-export are stable.

See docs/10 § Connections and docs/23-connections-and-auth.md for the
forthcoming concrete pool / scheme helpers.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


class AuthScheme(StrEnum):
    API_KEY = "api_key"
    BASIC_AUTH = "basic_auth"
    OAUTH2_CLIENT_CREDENTIALS = "oauth2_client_credentials"
    OAUTH2_REFRESH_TOKEN = "oauth2_refresh_token"
    JWT_BEARER = "jwt_bearer"
    SIGV4 = "sigv4"
    MTLS = "mtls"
    CUSTOM = "custom"


class ConnectionHealth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    latency_ms: int | None = None
    message: str = ""
    checked_at: datetime


class ConnectionDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ref: str
    slot: str
    auth_scheme: AuthScheme
    config_hash: str
    principal: str | None = None
    redacted_config: dict[str, Any] = Field(default_factory=dict)


class ConnectionContext(BaseModel):
    """Per-call context handed to a ConnectionFactory."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    pool_logger: Any = None
    tracer: Any = None
    cancel_token: Any = None


@runtime_checkable
class Connection[T_co](Protocol):
    ref: str
    slot: str

    @property
    def client(self) -> T_co: ...

    async def health(self) -> ConnectionHealth: ...


@runtime_checkable
class ConnectionFactory(Protocol):
    async def __call__(
        self,
        config: BaseModel,
        credentials: Any,
        ctx: ConnectionContext,
    ) -> Connection[Any]: ...


@runtime_checkable
class ConnectionAccessor(Protocol):
    async def get(self, slot: str) -> Connection[Any]: ...
    async def health(self, slot: str) -> ConnectionHealth: ...
    def descriptor(self, slot: str) -> ConnectionDescriptor: ...


@runtime_checkable
class ConnectionPool(Protocol):
    async def acquire(
        self,
        ref: str,
        config_hash: str,
        project: str,
        factory: ConnectionFactory,
        factory_args: Any,
    ) -> Connection[Any]: ...

    async def release(self, conn: Connection[Any]) -> None: ...
    async def refresh(self, ref: str, project: str) -> None: ...
    async def evict(self, ref: str, project: str | None = None) -> None: ...
    async def close_all(self) -> None: ...


__all__ = [
    "AuthScheme",
    "Connection",
    "ConnectionAccessor",
    "ConnectionContext",
    "ConnectionDescriptor",
    "ConnectionFactory",
    "ConnectionHealth",
    "ConnectionPool",
]
