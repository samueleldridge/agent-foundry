"""Token-bucket rate limiter tests (docs/85 § Cross-process rate limiting).

The Redis path runs against an in-test fake implementing the one operation
the bucket uses (``eval`` of the refill-and-take script) with the same
atomic semantics — redis-py is not a pinned dependency. The live-Redis
variant is the operator's manual step (docs/_manual_tests/phase_8.md).
"""

from __future__ import annotations

import asyncio
import itertools
import math
import time
from typing import Any

import httpx
import pytest

from foundry.core import (
    CredentialsRef,
    FoundryMessage,
    MessageRole,
    ResolvedCredentials,
    Session,
    TextBlock,
)
from foundry.core.errors import (
    ProviderRateLimitError,
    ProviderUnexpectedError,
    RunCancelled,
)
from foundry.providers import (
    InProcessTokenBucket,
    ModelBinding,
    RedisTokenBucket,
    build_rate_limiter,
    default_rate_limiter,
    reset_default_rate_limiter,
    resolve,
)


@pytest.fixture(autouse=True)
def _clean_default_limiter(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.delenv("FOUNDRY_RATE_LIMITER", raising=False)
    monkeypatch.delenv("FOUNDRY_RATE_LIMIT_RPS", raising=False)
    monkeypatch.delenv("FOUNDRY_RATE_LIMIT_BURST", raising=False)
    reset_default_rate_limiter()
    yield
    reset_default_rate_limiter()


# --- in-process bucket ---------------------------------------------------------


@pytest.mark.unit
async def test_in_process_bucket_caps_the_rate() -> None:
    bucket = InProcessTokenBucket(rate=50.0, capacity=5.0)
    started = time.monotonic()
    for _ in range(15):
        await bucket.acquire("anthropic:claude-haiku-4-5")
    elapsed = time.monotonic() - started
    # 5 burst tokens are free; the remaining 10 refill at 50/s → >= 0.2s.
    assert elapsed >= 0.18, f"15 grants in {elapsed:.3f}s beats the configured rate"


@pytest.mark.unit
async def test_in_process_bucket_keys_are_independent() -> None:
    bucket = InProcessTokenBucket(rate=1.0, capacity=1.0)
    started = time.monotonic()
    await bucket.acquire("anthropic:model-a")
    await bucket.acquire("openai:model-b")
    assert time.monotonic() - started < 0.5, "distinct keys must not contend"


@pytest.mark.unit
async def test_acquire_timeout_raises_structured_rate_limit_error() -> None:
    bucket = InProcessTokenBucket(rate=0.1, capacity=1.0)
    await bucket.acquire("k")  # drains the single token
    with pytest.raises(ProviderRateLimitError) as excinfo:
        await bucket.acquire("k", timeout_s=0.05)
    assert excinfo.value.context["key"] == "k"


@pytest.mark.unit
async def test_cancellation_wins_over_deferred_wait() -> None:
    bucket = InProcessTokenBucket(rate=0.1, capacity=1.0)
    await bucket.acquire("k")
    session = Session.new(project="t")

    async def cancel_soon() -> None:
        await asyncio.sleep(0.05)
        session.cancel_token.cancel("user_abort")

    started = time.monotonic()
    async with asyncio.TaskGroup() as tg:
        tg.create_task(cancel_soon())
        with pytest.raises(RunCancelled):
            await bucket.acquire("k", session=session, timeout_s=30.0)
    assert time.monotonic() - started < 1.0, "cancel must abort the wait promptly"


# --- fake redis + shared bucket ---------------------------------------------------


class FakeRedis:
    """Minimal fake of the ONE operation RedisTokenBucket uses: ``eval`` of
    the refill-and-take script. Executes the same token-bucket semantics in
    Python, atomically under a lock, over a store shared by every client
    ('worker') pointing at this instance. Mirrors the script's contract:
    time comes from the SERVER clock (this instance's monotonic clock —
    client wall clocks never enter), ``last`` only ever advances, and both
    keys carry a TTL (expired keys read as missing → full bucket)."""

    def __init__(self) -> None:
        self.store: dict[str, float] = {}
        self.ttls: dict[str, float] = {}
        """key → TTL seconds from the most recent SET ... EX."""
        self._expires_at_ms: dict[str, float] = {}
        self._lock = asyncio.Lock()
        self.eval_calls = 0

    def _server_now_ms(self) -> float:
        return time.monotonic() * 1000.0

    def _get(self, key: str, now_ms: float) -> float | None:
        if key in self.store and now_ms >= self._expires_at_ms.get(
            key, float("inf")
        ):
            del self.store[key]  # TTL expiry: reads as missing
        return self.store.get(key)

    def _set(self, key: str, value: float, ttl_s: float, now_ms: float) -> None:
        self.store[key] = value
        self.ttls[key] = ttl_s
        self._expires_at_ms[key] = now_ms + ttl_s * 1000.0

    async def eval(
        self, script: str, numkeys: int, *keys_and_args: Any
    ) -> list[int]:
        assert numkeys == 2
        tokens_key, last_key = keys_and_args[0], keys_and_args[1]
        rate_ms, capacity, cost, ttl_s = (
            float(a) for a in keys_and_args[2:6]
        )
        async with self._lock:
            self.eval_calls += 1
            now_ms = self._server_now_ms()
            stored_tokens = self._get(tokens_key, now_ms)
            tokens = capacity if stored_tokens is None else stored_tokens
            stored_last = self._get(last_key, now_ms)
            last = now_ms if stored_last is None else stored_last
            elapsed = now_ms - last
            if elapsed > 0:
                tokens = min(capacity, tokens + elapsed * rate_ms)
                last = now_ms  # advances only — never rewinds
            if tokens >= cost:
                self._set(tokens_key, tokens - cost, ttl_s, now_ms)
                self._set(last_key, last, ttl_s, now_ms)
                return [1, 0]
            self._set(tokens_key, tokens, ttl_s, now_ms)
            self._set(last_key, last, ttl_s, now_ms)
            wait_ms = int((cost - tokens) / rate_ms) + 1
            return [0, wait_ms]


@pytest.mark.unit
async def test_three_workers_share_one_redis_bucket_under_load() -> None:
    """Phase 8 exit gate (scaled): 3 'workers' (3 bucket clients, one shared
    fake-Redis store) under synthetic load keep the AGGREGATE grant rate
    under the configured limit over the measurement window."""
    fake = FakeRedis()
    rate, capacity = 40.0, 5.0
    workers = [
        RedisTokenBucket("redis://shared", rate, capacity, client=fake)
        for _ in range(3)
    ]
    grants: list[float] = []
    duration_s = 0.7
    deadline = time.monotonic() + duration_s

    async def hammer(bucket: RedisTokenBucket) -> None:
        while time.monotonic() < deadline:
            await bucket.acquire("anthropic:claude-opus-4-7", timeout_s=5.0)
            grants.append(time.monotonic())

    async with asyncio.TaskGroup() as tg:
        for bucket in workers:
            # Two concurrent callers per worker — 6 hammering tasks total.
            tg.create_task(hammer(bucket))
            tg.create_task(hammer(bucket))

    # Hammer loops check the deadline BEFORE acquiring, so the final grants
    # can land after it — bound against the actual first→last grant span.
    span_s = max(grants) - min(grants)
    allowed = rate * span_s + capacity
    assert len(grants) <= allowed + 2, (
        f"{len(grants)} aggregate grants over {span_s:.2f}s exceeds the "
        f"shared limit of ~{allowed:.0f}"
    )
    # And the limiter actually granted work (it isn't just blocking).
    assert len(grants) >= rate * duration_s * 0.5
    # Sliding-window check: no 0.25s window grants more than its share.
    window = 0.25
    for i, t0 in enumerate(grants):
        in_window = [t for t in grants[i:] if t - t0 <= window]
        assert len(in_window) <= rate * window + capacity + 2


@pytest.mark.unit
async def test_skewed_client_clocks_cannot_over_admit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression (Phase 8 review): the old script trusted client-supplied
    ``now`` and rewrote ``last`` even when elapsed <= 0 — a slow-clock
    worker rewound ``last`` and fast-clock workers re-credited the same
    refill window, over-admitting without bound. The fix sources time
    from the server inside the script, so client wall clocks are inert:
    even with time.time() swinging +/-1h between calls, aggregate admission
    stays within rate*span + burst."""
    real_time = time.time
    skew = itertools.cycle([-3600.0, 0.0, 3600.0])
    monkeypatch.setattr(time, "time", lambda: real_time() + next(skew))

    fake = FakeRedis()
    rate, capacity = 40.0, 5.0
    workers = [
        RedisTokenBucket("redis://shared", rate, capacity, client=fake)
        for _ in range(3)
    ]
    grants: list[float] = []
    deadline = time.monotonic() + 0.5

    async def hammer(bucket: RedisTokenBucket) -> None:
        while time.monotonic() < deadline:
            await bucket.acquire("anthropic:claude-opus-4-7", timeout_s=5.0)
            grants.append(time.monotonic())

    async with asyncio.TaskGroup() as tg:
        for bucket in workers:
            tg.create_task(hammer(bucket))
            tg.create_task(hammer(bucket))

    span_s = max(grants) - min(grants)
    allowed = rate * span_s + capacity
    assert len(grants) <= allowed + 2, (
        f"{len(grants)} grants over {span_s:.2f}s with skewed client "
        f"clocks exceeds the shared limit of ~{allowed:.0f} — the bucket "
        "is trusting client time again"
    )
    assert len(grants) >= 1  # the limiter still grants work


@pytest.mark.unit
async def test_bucket_keys_carry_a_ttl() -> None:
    """Hygiene (Phase 8 review): every bucket key is SET with EX =
    ceil(burst/rate) + 60s so idle keys expire instead of accumulating."""
    fake = FakeRedis()
    bucket = RedisTokenBucket("redis://s", rate=2.0, capacity=6.0, client=fake)
    assert bucket.ttl_s == math.ceil(6.0 / 2.0) + 60
    await bucket.acquire("anthropic:model-x")
    assert fake.ttls["foundry:rl:anthropic:model-x:tokens"] == bucket.ttl_s
    assert fake.ttls["foundry:rl:anthropic:model-x:last"] == bucket.ttl_s


@pytest.mark.unit
async def test_redis_unavailable_fails_closed() -> None:
    class DownRedis:
        async def eval(self, *args: Any, **kwargs: Any) -> Any:
            raise ConnectionError("connection refused")

    bucket = RedisTokenBucket("redis://down", 10.0, client=DownRedis())
    with pytest.raises(ProviderUnexpectedError) as excinfo:
        await bucket.acquire("k")
    assert excinfo.value.context["rate_limiter"] == "unavailable"


# --- env selection ------------------------------------------------------------------


@pytest.mark.unit
def test_build_rate_limiter_selection(monkeypatch: pytest.MonkeyPatch) -> None:
    assert build_rate_limiter("") is None
    assert build_rate_limiter("off") is None
    limiter = build_rate_limiter("in_process")
    assert isinstance(limiter, InProcessTokenBucket)
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_RPS", "3.5")
    monkeypatch.setenv("FOUNDRY_RATE_LIMIT_BURST", "7")
    redis_limiter = build_rate_limiter("redis://localhost:6379/0")
    assert isinstance(redis_limiter, RedisTokenBucket)
    assert redis_limiter.rate == 3.5
    assert redis_limiter.capacity == 7.0
    with pytest.raises(ProviderUnexpectedError):
        build_rate_limiter("carrier-pigeon")


@pytest.mark.unit
def test_default_limiter_reads_env_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FOUNDRY_RATE_LIMITER", "in_process")
    reset_default_rate_limiter()
    first = default_rate_limiter()
    assert isinstance(first, InProcessTokenBucket)
    assert default_rate_limiter() is first
    reset_default_rate_limiter()


# --- adapter integration ---------------------------------------------------------


class _FakeSecrets:
    def resolve(self, ref: CredentialsRef | None) -> ResolvedCredentials:
        return ResolvedCredentials(kind="env", secret="fake-key-for-tests")


class _RecordingLimiter:
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def acquire(
        self,
        key: str,
        cost: float = 1.0,
        *,
        session: Session | None = None,
        timeout_s: float = 60.0,
    ) -> None:
        self.keys.append(key)


@pytest.mark.unit
async def test_provider_generate_consults_the_limiter_per_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "content": [{"type": "text", "text": '{"greeting": "hi"}'}],
                "stop_reason": "end_turn",
                "model": "claude-haiku-4-5",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

    adapter = resolve(
        ModelBinding(provider="anthropic", model="claude-haiku-4-5"),
        _FakeSecrets(),
        transport=httpx.MockTransport(handler),
    )
    limiter = _RecordingLimiter()
    # The constructor override point (create_app / tests); resolve() call
    # sites get the env-configured default instead.
    adapter._rate_limiter = limiter  # type: ignore[attr-defined]
    messages = [
        FoundryMessage(role=MessageRole.USER, content=[TextBlock(text="hi")])
    ]
    await adapter.generate(messages, [])
    await adapter.generate(messages, [])
    assert limiter.keys == [
        "anthropic:claude-haiku-4-5",
        "anthropic:claude-haiku-4-5",
    ]
