"""custom scheme: escape hatch for exotic auth flows (docs/23 § custom).

The connection's auth.py supplies an arbitrary async callable; this helper
only validates the shape and passes credentials through. Documented as
unportable; the meta-agent never scaffolds it without explicit human
instruction.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

from pydantic import BaseModel, ConfigDict

from foundry.core.connection import ResolvedConnectionCredentials
from foundry.core.errors import ConnectionConfigError

CustomAuthFn = Callable[..., Awaitable[Any]]


class CustomAuthConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    description: str = ""
    """What the custom flow does — surfaces in reviews and descriptors."""


def validate_custom_auth(fn: object, *, where: str) -> CustomAuthFn:
    if not inspect.iscoroutinefunction(fn):
        raise ConnectionConfigError(
            f"custom auth callable at {where} must be async",
            context={"where": where},
        )
    return fn


async def apply(
    fn: CustomAuthFn,
    config: CustomAuthConfig,
    credentials: ResolvedConnectionCredentials,
    **kwargs: Any,
) -> Any:
    return await fn(config=config, credentials=credentials, **kwargs)


__all__ = ["CustomAuthConfig", "CustomAuthFn", "apply", "validate_custom_auth"]
