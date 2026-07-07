"""Shared primitive types: RunId, CredentialsRef, CacheControl.

These are the small, stable types that live at the bottom of the dependency
graph. ``ArtifactRef`` lives in ``foundry.config.refs`` because resolution is
a config-layer concern; the core layer only needs the string form.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, GetCoreSchemaHandler
from pydantic_core import CoreSchema, core_schema

# --- RunId (ULID-style) -----------------------------------------------------

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _encode_ulid(ms: int, rand: bytes) -> str:
    # 48 bits of time + 80 bits of randomness, Crockford-base32 (26 chars).
    if len(rand) != 10:
        raise ValueError("ULID randomness must be 10 bytes")
    n = (ms & ((1 << 48) - 1)) << 80
    n |= int.from_bytes(rand, "big")
    out: list[str] = []
    for _ in range(26):
        out.append(_CROCKFORD[n & 0x1F])
        n >>= 5
    return "".join(reversed(out))


# Monotonicity state for RunId.new(): ids minted within the same millisecond
# increment the random component so ids always sort ascending in generation
# order (docs/10 § Test expectations, unit test 7).
_last_ulid: tuple[int, int] | None = None
_ulid_lock = threading.Lock()


class RunId(str):
    """Typed ULID string. Use ``RunId.new()`` to mint."""

    __slots__ = ()

    @classmethod
    def new(cls) -> RunId:
        global _last_ulid
        ms = int(time.time() * 1000)
        with _ulid_lock:
            if _last_ulid is not None and _last_ulid[0] >= ms:
                ms = _last_ulid[0]
                rand_int = (_last_ulid[1] + 1) & ((1 << 80) - 1)
            else:
                rand_int = int.from_bytes(os.urandom(10), "big")
            _last_ulid = (ms, rand_int)
        return cls(_encode_ulid(ms, rand_int.to_bytes(10, "big")))

    @classmethod
    def validate(cls, v: str) -> RunId:
        if not isinstance(v, str) or len(v) != 26:
            raise ValueError(f"invalid RunId: {v!r}")
        for ch in v:
            if ch not in _CROCKFORD:
                raise ValueError(f"invalid RunId char {ch!r} in {v!r}")
        return cls(v)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> CoreSchema:
        # Validate as a str; coerce to RunId on the way out.
        return core_schema.no_info_after_validator_function(
            cls.validate, core_schema.str_schema()
        )


# --- CredentialsRef ---------------------------------------------------------


class CredentialsRef(BaseModel):
    """Pointer to a secret. Never the secret itself.

    Resolved by ``foundry.config.secrets.SecretsProvider`` at provider build
    time. See docs/11-provider-abstraction.md § Credentials and secrets.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["env", "aws_profile", "secret_manager", "default"]
    value: str | None = None


class ResolvedCredentials(BaseModel):
    """Opaque, redact-on-print credentials handed to a provider adapter."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["env", "aws_profile", "secret_manager", "default"]
    secret: str | None = None

    def __repr__(self) -> str:
        return f"ResolvedCredentials(kind={self.kind!r}, secret=<redacted>)"


# --- CacheControl (per-block prompt-cache marker) ---------------------------


class CacheControl(BaseModel):
    """Provider-neutral cache-control marker for a content block."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    type: Literal["ephemeral"] = "ephemeral"


__all__ = [
    "CacheControl",
    "CredentialsRef",
    "ResolvedCredentials",
    "RunId",
]
