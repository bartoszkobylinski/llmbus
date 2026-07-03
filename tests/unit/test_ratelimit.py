"""Unit tests for global per-provider rate limiting (ARCHITECTURE.md §6).

The bucket math is pure and time is injected, so every case is deterministic —
no real sleeping, no wall clock. Numbers are chosen so waits come out to clean
values that pin down the exact arithmetic (a swapped operator or dropped refill
cap changes them).
"""

import asyncio
import time

import pytest

from llmbus.ratelimit import (
    ProviderLimiter,
    ProviderLimits,
    RateLimiter,
    TokenBucket,
    UnknownProviderError,
)


class FakeClock:
    """A hand-cranked monotonic clock; `advance` is the only way time moves."""

    def __init__(self, t: float = 0.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt: float):
        self.t += dt


class RecordingSleep:
    """Stands in for `asyncio.sleep`; records every requested duration instead of
    actually waiting."""

    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


# --- TokenBucket -------------------------------------------------------------


def test_bucket_starts_full_and_grants_a_full_burst_without_waiting():
    bucket = TokenBucket(rate=10, capacity=60, now=0)
    assert bucket.reserve(0, 60) == 0.0  # the whole burst is available up front


def test_bucket_wait_is_deficit_over_rate():
    bucket = TokenBucket(rate=10, capacity=60, now=0)
    assert bucket.reserve(0, 60) == 0.0  # drain to exactly empty
    # 10 tokens over, refilling at 10/s → exactly 1 second to cover the deficit.
    assert bucket.reserve(0, 10) == 1.0


def test_bucket_refills_continuously_with_elapsed_time():
    bucket = TokenBucket(rate=10, capacity=60, now=0)
    assert bucket.reserve(0, 60) == 0.0  # empty at t=0
    # 3s later, 30 tokens have refilled; asking for 30 is free, 31 waits 0.1s.
    assert bucket.reserve(3, 30) == 0.0
    assert bucket.reserve(3, 1) == pytest.approx(0.1)


def test_bucket_reservation_serializes_concurrent_callers():
    bucket = TokenBucket(rate=10, capacity=60, now=0)
    assert bucket.reserve(0, 60) == 0.0
    # Two callers reserve past empty at the same instant; the second inherits the
    # first's deficit and waits longer — this ordering is what makes concurrent
    # acquire() fair without a lock.
    assert bucket.reserve(0, 10) == 1.0
    assert bucket.reserve(0, 10) == 2.0


def test_bucket_refill_is_capped_at_capacity():
    bucket = TokenBucket(rate=10, capacity=60, now=0)
    assert bucket.reserve(0, 60) == 0.0  # drain
    assert bucket.reserve(0, 60) == 6.0  # -60 balance, wait 6s
    # Idle far longer than needed to recover: the bucket must cap at capacity, not
    # bank 1000s of unused tokens. So a fresh full burst is free...
    assert bucket.reserve(1000, 60) == 0.0
    # ...but only a full burst — the very next token proves it didn't overflow.
    assert bucket.reserve(1000, 1) == pytest.approx(0.1)


def test_bucket_ignores_non_advancing_or_backward_clock():
    bucket = TokenBucket(rate=10, capacity=60, now=5)
    assert bucket.reserve(5, 60) == 0.0
    # A clock that doesn't move (or moves backward) must not refill: the deficit
    # keeps accumulating instead of being silently forgiven.
    assert bucket.reserve(5, 10) == 1.0
    assert bucket.reserve(4, 10) == 2.0


def test_bucket_zero_amount_is_free_when_positive():
    bucket = TokenBucket(rate=10, capacity=60, now=0)
    assert bucket.reserve(0, 0) == 0.0


def test_bucket_rejects_negative_amount():
    bucket = TokenBucket(rate=10, capacity=60, now=0)
    with pytest.raises(ValueError, match=r"^amount must be non-negative$"):
        bucket.reserve(0, -1)


@pytest.mark.parametrize(("rate", "capacity"), [(0, 60), (-1, 60), (10, 0), (10, -1)])
def test_bucket_rejects_non_positive_rate_or_capacity(rate, capacity):
    # Anchored so a mutated message (mutmut wraps strings as "XX…XX") no longer
    # matches — the substring "positive" would still slip through a loose match.
    with pytest.raises(ValueError, match=r"^rate and capacity must be positive$"):
        TokenBucket(rate=rate, capacity=capacity, now=0)


def test_bucket_accepts_fractional_rate_and_capacity():
    # 30 requests/min → rate 0.5/s, so sub-1 rates and capacities are real and
    # must be accepted — the positivity check is `<= 0`, not `<= 1`.
    bucket = TokenBucket(rate=0.5, capacity=0.5, now=0)
    assert bucket.reserve(0, 0.5) == 0.0  # the full sub-1 burst is free
    assert bucket.reserve(0, 0.5) == 1.0  # 0.5 over at 0.5/s → 1s


# --- ProviderLimits ----------------------------------------------------------


def test_provider_limits_accepts_positive_ceilings():
    limits = ProviderLimits(requests_per_min=60, tokens_per_min=6000)
    assert (limits.requests_per_min, limits.tokens_per_min) == (60, 6000)


@pytest.mark.parametrize(
    ("requests_per_min", "tokens_per_min"),
    [(0, 6000), (-1, 6000), (60, 0), (60, -1)],
)
def test_provider_limits_rejects_non_positive_ceilings(requests_per_min, tokens_per_min):
    with pytest.raises(ValueError, match=r"^per-minute limits must be positive$"):
        ProviderLimits(requests_per_min=requests_per_min, tokens_per_min=tokens_per_min)


def test_provider_limits_is_frozen():
    limits = ProviderLimits(requests_per_min=60, tokens_per_min=6000)
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError is dataclass-internal
        limits.requests_per_min = 1


# --- ProviderLimiter ---------------------------------------------------------


def test_provider_limiter_is_token_bound_when_tokens_are_the_constraint():
    # tokens: 6000/min → 100/s, cap 6000. requests: 60/min → 1/s, cap 60.
    limiter = ProviderLimiter(ProviderLimits(requests_per_min=60, tokens_per_min=6000), now=0)
    assert limiter.reserve(0, tokens=6000) == 0.0  # first job drains the token bucket
    # Second job at the same instant: requests are fine (0 wait) but tokens are
    # 6000 over → 60s. The stricter dimension wins.
    assert limiter.reserve(0, tokens=6000) == 60.0


def test_provider_limiter_is_request_bound_when_requests_are_the_constraint():
    limiter = ProviderLimiter(ProviderLimits(requests_per_min=60, tokens_per_min=6000), now=0)
    for _ in range(60):  # exhaust the request bucket with cheap jobs
        assert limiter.reserve(0, tokens=10) == 0.0
    # 61st request is 1 over at 1/s → 1s, while tokens are nowhere near their cap.
    assert limiter.reserve(0, tokens=10) == 1.0


def test_provider_limiter_charges_both_buckets_independently():
    limiter = ProviderLimiter(ProviderLimits(requests_per_min=60, tokens_per_min=6000), now=0)
    for _ in range(60):  # zero-token jobs never touch the token bucket...
        assert limiter.reserve(0, tokens=0) == 0.0
    # ...yet requests still get charged, so the limit binds on requests alone.
    assert limiter.reserve(0, tokens=0) == 1.0


def test_provider_limiter_honours_explicit_request_count():
    limiter = ProviderLimiter(ProviderLimits(requests_per_min=60, tokens_per_min=6000), now=0)
    # Spending the whole request burst in one call empties it; the next request waits.
    assert limiter.reserve(0, tokens=0, requests=60) == 0.0
    assert limiter.reserve(0, tokens=0, requests=1) == 1.0


# --- RateLimiter -------------------------------------------------------------


def _limits():
    return {
        "openai": ProviderLimits(requests_per_min=60, tokens_per_min=6000),
        "anthropic": ProviderLimits(requests_per_min=60, tokens_per_min=6000),
    }


def test_ratelimiter_reserve_routes_to_the_named_provider():
    limiter = RateLimiter(_limits(), clock=FakeClock(0))
    assert limiter.reserve("openai", 6000) == 0.0
    assert limiter.reserve("openai", 6000) == 60.0


def test_ratelimiter_providers_are_isolated():
    limiter = RateLimiter(_limits(), clock=FakeClock(0))
    assert limiter.reserve("openai", 6000) == 0.0
    assert limiter.reserve("openai", 6000) == 60.0  # openai now saturated
    assert limiter.reserve("anthropic", 6000) == 0.0  # anthropic untouched


def test_ratelimiter_unknown_provider_raises():
    limiter = RateLimiter({"openai": ProviderLimits(60, 6000)}, clock=FakeClock(0))
    with pytest.raises(UnknownProviderError, match="anthropic"):
        limiter.reserve("anthropic", 10)


def test_ratelimiter_uses_the_injected_clock():
    clock = FakeClock(0)
    limiter = RateLimiter(_limits(), clock=clock)
    assert limiter.reserve("openai", 6000) == 0.0
    assert limiter.reserve("openai", 6000) == 60.0  # 6000 over at 100/s
    clock.advance(30)  # half the wait elapses
    assert limiter.reserve("openai", 0) == 30.0  # remaining deficit, no new tokens spent


async def test_acquire_does_not_sleep_when_within_limits():
    sleep = RecordingSleep()
    limiter = RateLimiter(_limits(), clock=FakeClock(0), sleep=sleep)
    await limiter.acquire("openai", 6000)  # fits exactly on the first job
    assert sleep.calls == []


async def test_acquire_sleeps_exactly_the_required_wait():
    sleep = RecordingSleep()
    limiter = RateLimiter(_limits(), clock=FakeClock(0), sleep=sleep)
    await limiter.acquire("openai", 6000)  # drains the token bucket, no sleep
    await limiter.acquire("openai", 6000)  # 6000 over at 100/s → 60s
    assert sleep.calls == [60.0]


async def test_acquire_sleeps_even_a_sub_second_wait():
    # A small overage must still sleep: the gate is `wait > 0`, not `wait > 1`.
    sleep = RecordingSleep()
    limiter = RateLimiter(_limits(), clock=FakeClock(0), sleep=sleep)
    await limiter.acquire("openai", 6000)  # drain, no sleep
    await limiter.acquire("openai", 50)  # 50 over at 100/s → 0.5s
    assert sleep.calls == [0.5]


async def test_acquire_propagates_unknown_provider():
    limiter = RateLimiter({"openai": ProviderLimits(60, 6000)}, clock=FakeClock(0))
    with pytest.raises(UnknownProviderError):
        await limiter.acquire("anthropic", 10)


def test_ratelimiter_defaults_to_monotonic_clock_and_asyncio_sleep():
    limiter = RateLimiter(_limits())
    assert limiter._clock is time.monotonic
    assert limiter._sleep is asyncio.sleep
