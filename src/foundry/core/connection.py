"""Connection protocols + credential value types.

The concrete pool / scheme helpers live in ``foundry.connections`` and
``foundry.auth`` (Phase 2a). This module holds only the cross-layer
primitives: protocols, the descriptor, and the redact-on-print credential
wrappers. See docs/10 § Connections and docs/23-connections-and-auth.md.
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


class SecretValue:
    """Wrapper around a secret string that never prints or serialises its
    value. Factories and auth-scheme helpers call ``.reveal()`` — the ONLY
    read path (docs/23 § Credentials resolution)."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __str__(self) -> str:
        return "<redacted>"

    def __repr__(self) -> str:
        return "<SecretValue redacted>"

    def __eq__(self, other: object) -> bool:
        # Identity-only equality: comparing secrets by value invites
        # timing-channel misuse and accidental logging in assertions.
        return self is other

    def __hash__(self) -> int:
        return id(self)


class ResolvedConnectionCredentials:
    """Typed, opaque bundle of scheme-specific secret fields handed to a
    ConnectionFactory (e.g. ``api_key``; ``client_id`` + ``client_secret``;
    ``username`` + ``password``). Redacted on print.

    Distinct from ``foundry.core.types.ResolvedCredentials`` (the Phase 1
    single-secret provider credential); connections need multi-field
    credentials per auth scheme.
    """

    __slots__ = ("fields", "principal", "scheme")

    def __init__(
        self,
        scheme: AuthScheme,
        fields: dict[str, SecretValue],
        principal: str | None = None,
    ) -> None:
        self.scheme = scheme
        self.fields = fields
        self.principal = principal

    def require(self, name: str) -> SecretValue:
        """Fetch a named credential field; structured error when absent."""
        value = self.fields.get(name)
        if value is None:
            # Import here keeps module-load order simple (errors ← nothing).
            from foundry.core.errors import ConnectionAuthError

            raise ConnectionAuthError(
                f"credentials for scheme {self.scheme.value!r} are missing the "
                f"required field {name!r} (present: {sorted(self.fields)})",
                context={"scheme": self.scheme.value, "missing_field": name,
                         "present_fields": sorted(self.fields)},
            )
        return value

    def __str__(self) -> str:
        return (
            f"ResolvedConnectionCredentials(scheme={self.scheme.value!r}, "
            f"fields=<redacted:{sorted(self.fields)}>)"
        )

    __repr__ = __str__


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
    http_transport: Any = None
    """Optional httpx.AsyncBaseTransport override for factories that build
    httpx clients. Lets the whole connection stack run against
    httpx.MockTransport in tests (same pattern as ProviderAdapter)."""


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

    async def on_auth_error(self) -> bool:
        """Handle a ConnectionAuthError raised mid-tool-call: evict pool
        entries for acquired slots whose refresh policy is ``on_auth_error``.
        Returns True if anything was evicted (the dispatcher then retries the
        handler once). See docs/23 § Refresh."""
        ...

    async def release_all(self) -> None:
        """Release every connection acquired through this accessor (end of
        tool call). The pool owns lifecycle; handlers never call this."""
        ...


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
    "ResolvedConnectionCredentials",
    "SecretValue",
]
