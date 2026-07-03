"""Global per-provider rate limiting for the bus (ARCHITECTURE.md §6).

The whole point of the bus is that OpenAI and Anthropic are called *centrally*,
so their rate limits are enforced in one place instead of every producer
guessing. Each provider gets its own limiter with two token buckets — one for
requests/min, one for tokens/min — because providers cap both dimensions
independently and either can be the binding constraint.

The limits themselves are policy and live in config (`.env`/`config.py`, §10);
this module is only the *mechanism*. A `RateLimiter` is built from a
`{provider: ProviderLimits}` map, and `acquire()` is what the worker awaits
before each model call.

Time is injected, never read from the wall clock inside the math — `reserve()`
takes `now` explicitly, exactly as `cost.py` takes the job date. That keeps the
bucket arithmetic pure and deterministic (no `sleep`, no monotonic clock in the
hot path), so it can be unit- and mutation-tested without real time passing. The
thin async `acquire()` wrapper is the only part that touches a clock or sleeps,
and both are injected too so even it stays testable.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass


class UnknownProviderError(KeyError):
    """No limiter is configured for a provider — raised rather than silently
    letting an unlimited stream of calls past the bus."""


@dataclass(frozen=True)
class ProviderLimits:
    """Per-provider ceilings, as billed by the provider: requests and tokens per
    minute. Both must be positive — a zero/negative ceiling would stall the
    provider forever, which is a config bug we want loud."""

    requests_per_min: float
    tokens_per_min: float

    def __post_init__(self) -> None:
        if self.requests_per_min <= 0 or self.tokens_per_min <= 0:
            raise ValueError("per-minute limits must be positive")


def _require_non_negative(amount: float) -> None:
    """Reject a negative bucket amount. Called *before* any bucket is charged so a
    bad reservation is all-or-nothing and never silently consumes capacity."""
    if amount < 0:
        raise ValueError("amount must be non-negative")


class TokenBucket:
    """A continuously-refilling token bucket.

    Fills at `rate` units/second up to `capacity` (the burst ceiling) and starts
    full. `reserve()` deducts immediately and may drive the balance negative:
    the deficit represents units promised to earlier callers, so a later caller
    sees a longer wait. That reservation is what serializes concurrent coroutines
    fairly — no lock needed, since `reserve()` never awaits and the event loop is
    single-threaded.
    """

    def __init__(self, rate: float, capacity: float, now: float) -> None:
        if rate <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self._rate = rate
        self._capacity = capacity
        self._tokens = capacity
        self._updated = now

    def reserve(self, now: float, amount: float) -> float:
        """Reserve `amount` units and return the seconds to wait before they are
        available (0.0 if immediately). The units are always granted; the wait is
        how long the caller must hold off to stay within the rate."""
        _require_non_negative(amount)
        # Refill, branch-free: clamp elapsed at 0 so a non-monotonic (backward)
        # clock can never *drain* the bucket, and `_updated` only ever moves
        # forward. A clamped-zero elapsed refills by nothing, so this matches an
        # explicit "only refill when time advanced" guard without the branch.
        elapsed = max(0.0, now - self._updated)
        self._tokens = min(self._capacity, self._tokens + elapsed * self._rate)
        self._updated = max(self._updated, now)
        # Spend, then the wait is the deficit spread over the refill rate — floored
        # at 0 while the balance is still non-negative.
        self._tokens -= amount
        deficit = max(0.0, -self._tokens)
        return deficit / self._rate


class ProviderLimiter:
    """Both dimensions of one provider's limit. A job spends one request and its
    (estimated) token count; the wait is the stricter of the two buckets."""

    def __init__(self, limits: ProviderLimits, now: float) -> None:
        self._requests = TokenBucket(limits.requests_per_min / 60, limits.requests_per_min, now)
        self._tokens = TokenBucket(limits.tokens_per_min / 60, limits.tokens_per_min, now)

    def reserve(self, now: float, tokens: float, requests: float = 1) -> float:
        """Reserve one job's requests and tokens; return the longer of the two
        required waits. Both buckets are always charged so neither limit is ever
        exceeded, even when only one is the binding constraint."""
        # Validate tokens before charging the request bucket: otherwise a job that
        # fails token validation would still consume a request slot, letting a bad
        # caller shrink capacity through failures. (Negative requests are already
        # atomic — that bucket is charged first and validates before mutating.)
        _require_non_negative(tokens)
        wait_requests = self._requests.reserve(now, requests)
        wait_tokens = self._tokens.reserve(now, tokens)
        return max(wait_requests, wait_tokens)


class RateLimiter:
    """Global registry of per-provider limiters and the bus's throttle point.

    Built from a `{provider: ProviderLimits}` map. `acquire()` reserves a job's
    budget and sleeps out any required wait; it is what the worker awaits before
    handing a job to a provider adapter. Clock and sleep are injected (defaulting
    to `time.monotonic`/`asyncio.sleep`) so the async path is testable without
    real time passing.
    """

    def __init__(
        self,
        limits: Mapping[str, ProviderLimits],
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._clock = clock
        self._sleep = sleep
        start = clock()
        self._limiters = {
            provider: ProviderLimiter(provider_limits, start)
            for provider, provider_limits in limits.items()
        }

    def _limiter_for(self, provider: str) -> ProviderLimiter:
        try:
            return self._limiters[provider]
        except KeyError:
            raise UnknownProviderError(provider) from None

    def reserve(self, provider: str, tokens: float) -> float:
        """Reserve one job's budget against `provider` and return the seconds to
        wait. The synchronous decision — `acquire()` is this plus the sleep."""
        return self._limiter_for(provider).reserve(self._clock(), tokens)

    async def acquire(self, provider: str, tokens: float) -> None:
        """Block until a job for `provider` may proceed within its rate limits."""
        wait = self.reserve(provider, tokens)
        if wait > 0:
            await self._sleep(wait)
