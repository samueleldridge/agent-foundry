"""Provider-call rate limiting — token buckets, in-process and Redis-backed.

docs/85 § Cross-process rate limiting: the provider org-level rate limit is
a shared resource; each worker's local retry loop doesn't know what the
other workers are doing. The gate sits in ``ProviderAdapter.generate``,
keyed ``<provider>:<model>``.

Two backends behind one protocol:

- :class:`InProcessTokenBucket` — per-process bucket; the default backend
  when rate limiting is enabled without a Redis URL. Correct for
  single-worker dev; NOT shared across workers.
- :class:`RedisTokenBucket` — multi-worker shared bucket. The
  refill-and-take step runs as an atomic Lua script over the two docs/85
  keys (``foundry:rl:<key>:tokens`` / ``:last``). ``redis`` is imported
  lazily (same policy as foundry.cache's optional backends); a missing
  package or an unreachable Redis FAILS CLOSED with a structured
  ``ProviderUnexpectedError(context={"rate_limiter": "unavailable"})`` —
  an uncoordinated stampede against the provider org limit is worse than
  a refused call (docs/85 § Failure modes).

Selection (docs/03 § Phase 8): ``FOUNDRY_RATE_LIMITER`` env var —
unset/empty/``off`` disables the gate entirely (Phase ≤7 behaviour);
``in_process`` activates the local bucket; ``redis://...`` activates the
shared bucket. Rates come from ``FOUNDRY_RATE_LIMIT_RPS`` (refill
tokens/second, default 10) and ``FOUNDRY_RATE_LIMIT_BURST`` (bucket
capacity, default = rps). Per-(provider,model) rate manifests
(~/.foundry/rate_limits.yaml) are a documented v1.1+ extension.

Cancellation is honoured: the deferred-wait sleep polls the session's
cancel token, so a cancelled run abandons its place in line instead of
sleeping through the backoff (docs/71 § Cancellation inside retries).
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from typing import Any, Protocol

from foundry.core import Session
from foundry.core.errors import (
    ProviderRateLimitError,
    ProviderUnexpectedError,
    RunCancelled,
)

_DEFAULT_RPS = 10.0
_DEFAULT_ACQUIRE_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 0.05
"""Upper bound on one deferred-wait sleep — keeps cancellation latency low."""


class RateLimiter(Protocol):
    """The provider adapter's gate. ``acquire`` returns when a permit is
    granted, raises ``ProviderRateLimitError`` when the wait exceeds
    ``timeout_s``, and ``RunCancelled`` when the session cancels first."""

    async def acquire(
        self,
        key: str,
        cost: float = 1.0,
        *,
        session: Session | None = None,
        timeout_s: float = _DEFAULT_ACQUIRE_TIMEOUT_S,
    ) -> None: ...


def _check_cancelled(session: Session | None) -> None:
    if session is not None and session.cancel_token.cancelled():
        raise RunCancelled(
            "run cancelled while waiting for a rate-limit permit",
            context={"reason": session.cancel_token.reason},
        )


async def _deferred_sleep(wait_s: float, session: Session | None) -> None:
    """Sleep up to ``wait_s`` in short slices, aborting the moment the
    session cancels — cancellation wins over backoff (docs/71)."""
    deadline = time.monotonic() + wait_s
    while True:
        _check_cancelled(session)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        await asyncio.sleep(min(remaining, _POLL_INTERVAL_S))


def _timeout_error(key: str, timeout_s: float) -> ProviderRateLimitError:
    return ProviderRateLimitError(
        f"rate-limit permit for {key!r} not granted within {timeout_s}s — "
        "the configured rate is saturated (docs/85 § Backpressure)",
        context={"key": key, "timeout_s": timeout_s},
    )


class InProcessTokenBucket:
    """Per-process token bucket, one bucket per key.

    ``rate`` tokens refill per second up to ``capacity``. Grants are
    FIFO-ish under contention (all waiters poll on the same interval);
    exact fairness is not guaranteed and not required (docs/85 open q 3).
    """

    def __init__(self, rate: float, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0 tokens/second")
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self._buckets: dict[str, tuple[float, float]] = {}
        """key -> (tokens, last_refill_monotonic)."""
        self._lock = asyncio.Lock()

    def _take(self, key: str, cost: float) -> float:
        """Refill then take. Returns 0.0 on grant, else the recommended
        wait in seconds. Caller holds the lock."""
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (self.capacity, now))
        tokens = min(self.capacity, tokens + (now - last) * self.rate)
        if tokens >= cost:
            self._buckets[key] = (tokens - cost, now)
            return 0.0
        self._buckets[key] = (tokens, now)
        return (cost - tokens) / self.rate

    async def acquire(
        self,
        key: str,
        cost: float = 1.0,
        *,
        session: Session | None = None,
        timeout_s: float = _DEFAULT_ACQUIRE_TIMEOUT_S,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            _check_cancelled(session)
            async with self._lock:
                wait_s = self._take(key, cost)
            if wait_s <= 0:
                return
            if time.monotonic() + wait_s > deadline:
                raise _timeout_error(key, timeout_s)
            await _deferred_sleep(wait_s, session)


# One round trip, atomic: refill from elapsed millis, grant if covered,
# else report the shortfall as a recommended wait (docs/85 § Redis token
# bucket). Time comes from redis TIME — the SERVER clock — never from the
# callers: with client-supplied timestamps a slow-clock worker rewinds
# ``last`` and lets fast-clock workers re-credit the same refill window.
# ``last`` only ever advances (elapsed <= 0 leaves it untouched), and both
# keys carry a TTL so idle buckets don't accumulate forever. (TIME before
# writes is fine: Redis >= 5 replicates script EFFECTS, not the script.)
# KEYS[1]=tokens KEYS[2]=last; ARGV = rate/ms, capacity, cost, ttl_s.
# Returns {granted(0|1), wait_ms}.
_ACQUIRE_LUA = """
local time = redis.call('TIME')
local now = tonumber(time[1]) * 1000 + math.floor(tonumber(time[2]) / 1000)
local tokens = tonumber(redis.call('GET', KEYS[1]) or ARGV[2])
local last = tonumber(redis.call('GET', KEYS[2]) or now)
local rate_ms = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local ttl_s = tonumber(ARGV[4])
local elapsed = now - last
if elapsed > 0 then
  tokens = math.min(capacity, tokens + elapsed * rate_ms)
  last = now
end
if tokens >= cost then
  redis.call('SET', KEYS[1], tokens - cost, 'EX', ttl_s)
  redis.call('SET', KEYS[2], last, 'EX', ttl_s)
  return {1, 0}
end
redis.call('SET', KEYS[1], tokens, 'EX', ttl_s)
redis.call('SET', KEYS[2], last, 'EX', ttl_s)
local wait_ms = math.ceil((cost - tokens) / rate_ms)
return {0, wait_ms}
"""


class RedisTokenBucket:
    """Multi-worker shared token bucket over Redis (docs/85).

    The atomic step is the Lua script above; this client does a bounded
    sleep-and-retry on "deferred". ``client`` injection exists for tests
    (a minimal fake implementing ``eval``); production builds the client
    lazily from the URL.
    """

    def __init__(
        self,
        url: str,
        rate: float,
        capacity: float | None = None,
        *,
        client: Any | None = None,
        key_prefix: str = "foundry:rl",
    ) -> None:
        if rate <= 0:
            raise ValueError("rate must be > 0 tokens/second")
        self._url = url
        self.rate = rate
        self.capacity = capacity if capacity is not None else rate
        self._client = client
        self._key_prefix = key_prefix
        self.ttl_s = math.ceil(self.capacity / self.rate) + 60
        """Bucket-key TTL: a full refill window + slack. An expired pair
        re-initialises to a full bucket, which is the correct steady
        state for a key idle that long."""

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            import redis.asyncio as redis_asyncio
        except ImportError as exc:
            raise ProviderUnexpectedError(
                "FOUNDRY_RATE_LIMITER points at Redis but the optional "
                "'redis' package is not installed (uv add redis) — "
                "failing closed",
                context={
                    "rate_limiter": "unavailable",
                    "missing_package": "redis",
                },
                cause=exc,
            ) from exc
        self._client = redis_asyncio.from_url(self._url)
        return self._client

    async def _try_acquire(self, key: str, cost: float) -> tuple[bool, float]:
        """One atomic refill-and-take. Returns (granted, wait_s)."""
        client = self._get_client()
        tokens_key = f"{self._key_prefix}:{key}:tokens"
        last_key = f"{self._key_prefix}:{key}:last"
        try:
            granted, wait_ms = await client.eval(
                _ACQUIRE_LUA,
                2,
                tokens_key,
                last_key,
                self.rate / 1000.0,
                self.capacity,
                cost,
                self.ttl_s,
            )
        except ProviderUnexpectedError:
            raise
        except Exception as exc:  # fail CLOSED (docs/85 § Failure modes)
            raise ProviderUnexpectedError(
                f"Redis rate limiter unavailable: {exc} — failing closed to "
                "avoid an uncoordinated stampede against the provider limit",
                context={"rate_limiter": "unavailable", "key": key},
                cause=exc,
            ) from exc
        return bool(int(granted)), float(wait_ms) / 1000.0

    async def acquire(
        self,
        key: str,
        cost: float = 1.0,
        *,
        session: Session | None = None,
        timeout_s: float = _DEFAULT_ACQUIRE_TIMEOUT_S,
    ) -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            _check_cancelled(session)
            granted, wait_s = await self._try_acquire(key, cost)
            if granted:
                return
            wait_s = max(wait_s, 0.001)
            if time.monotonic() + wait_s > deadline:
                raise _timeout_error(key, timeout_s)
            await _deferred_sleep(wait_s, session)


# --- env-driven default (the swap point, docs/03 § Phase 8) -----------------

_UNSET = object()
_default_limiter: Any = _UNSET


def build_rate_limiter(setting: str | None = None) -> RateLimiter | None:
    """Construct the limiter for a ``FOUNDRY_RATE_LIMITER`` value.

    ``None``/``""``/``"off"`` → no gate; ``"in_process"`` → local bucket;
    ``"redis://..."`` (or rediss://) → shared bucket. Anything else is a
    loud config error rather than a silently-disabled limiter.
    """
    value = (
        setting
        if setting is not None
        else os.environ.get("FOUNDRY_RATE_LIMITER", "")
    ).strip()
    if value in ("", "off", "none"):
        return None
    rate = float(os.environ.get("FOUNDRY_RATE_LIMIT_RPS", _DEFAULT_RPS))
    burst_raw = os.environ.get("FOUNDRY_RATE_LIMIT_BURST")
    capacity = float(burst_raw) if burst_raw else None
    if value == "in_process":
        return InProcessTokenBucket(rate, capacity)
    if value.startswith(("redis://", "rediss://", "unix://")):
        return RedisTokenBucket(value, rate, capacity)
    raise ProviderUnexpectedError(
        f"FOUNDRY_RATE_LIMITER={value!r} is not recognised; expected "
        "'in_process', a redis:// URL, or empty/'off'",
        context={"rate_limiter": value},
    )


def default_rate_limiter() -> RateLimiter | None:
    """The process-wide limiter, built once from the environment. Every
    ProviderAdapter without an explicit ``rate_limiter`` consults this."""
    global _default_limiter
    if _default_limiter is _UNSET:
        _default_limiter = build_rate_limiter()
    return _default_limiter  # type: ignore[no-any-return]


def reset_default_rate_limiter() -> None:
    """Drop the cached default (tests; env changes at runtime)."""
    global _default_limiter
    _default_limiter = _UNSET


__all__ = [
    "InProcessTokenBucket",
    "RateLimiter",
    "RedisTokenBucket",
    "build_rate_limiter",
    "default_rate_limiter",
    "reset_default_rate_limiter",
]
