"""Retry/backoff policy, error classification, and the worker's run policy
(ARCHITECTURE.md §6).

The worker retries a model call on transient failures — 429, 5xx, timeouts,
connection drops — with exponential backoff and full jitter, giving up after a
bounded number of attempts. Everything in this module is **pure**: no SDK import,
no clock, no sleep. The policy types carry validated numbers, `is_retryable`
classifies an exception, and `backoff_delay` computes one wait from an injected
random draw. `processing.py`'s async loop supplies the real sleep and the real
exceptions, so this reliability-critical logic sits in the mutation gate instead
of integration-only code.

`is_retryable` **duck-types** the exception rather than importing the OpenAI /
Anthropic SDKs (which the adapters deliberately never import either, §7): a
transient HTTP failure exposes a `status_code` (both SDKs' `APIStatusError`
carries one), a slow call raises a `TimeoutError` (`asyncio.TimeoutError` is an
alias of it since 3.11), and a dropped connection surfaces as an
`APIConnectionError` / `APITimeoutError` — matched by class name so no SDK type
is needed here.
"""

from __future__ import annotations

import dataclasses

# HTTP statuses worth retrying: 408 Request Timeout, 409 Conflict, 429 Too Many
# Requests, plus any 5xx (checked separately). Every other 4xx is a caller/bug
# error (bad key, malformed request) — retrying just repeats the same failure and
# burns the budget, so it is terminal.
_RETRYABLE_STATUS: frozenset[int] = frozenset({408, 409, 429})

# SDK exception *class names* that mean a transient transport failure but carry no
# `status_code` and are not builtin `TimeoutError`/`ConnectionError` subclasses.
# Matched by name to keep this module free of the optional OpenAI/Anthropic SDKs.
_RETRYABLE_EXC_NAMES: frozenset[str] = frozenset({"APIConnectionError", "APITimeoutError"})


def _validate_retry_policy(max_attempts: int, base_delay_s: float, max_delay_s: float) -> None:
    """Reject an unusable retry policy (kept a module function, not a dataclass
    method, so the mutation gate reaches it — mutmut skips `@dataclass` methods)."""
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if base_delay_s <= 0:
        raise ValueError("base_delay_s must be positive")
    if max_delay_s < base_delay_s:
        raise ValueError("max_delay_s must be at least base_delay_s")


@dataclasses.dataclass(frozen=True)
class RetryPolicy:
    """How hard the worker retries a transient failure.

    `max_attempts` counts the initial call too — `max_attempts=1` means no retry,
    `max_attempts=4` means one call plus three retries. Backoff for retry number
    `i` (0-based) is `base_delay_s * 2**i`, capped at `max_delay_s`, then jittered.
    """

    max_attempts: int
    base_delay_s: float
    max_delay_s: float

    def __post_init__(self) -> None:
        _validate_retry_policy(self.max_attempts, self.base_delay_s, self.max_delay_s)


def _validate_worker_policy(job_timeout_s: float, default_output_tokens: int) -> None:
    """Reject an unusable worker policy (module function, not a dataclass method —
    same mutation-gate reason as `_validate_retry_policy`)."""
    if job_timeout_s <= 0:
        raise ValueError("job_timeout_s must be positive")
    if default_output_tokens < 0:
        raise ValueError("default_output_tokens must be non-negative")


@dataclasses.dataclass(frozen=True)
class WorkerPolicy:
    """The worker's per-job run policy: retry behaviour, the per-attempt timeout,
    and the output-token estimate used for rate-limit reservation when a job does
    not set `max_tokens` (§6). All of it is config (`.env`), never hardcoded."""

    retry: RetryPolicy
    job_timeout_s: float
    default_output_tokens: int

    def __post_init__(self) -> None:
        _validate_worker_policy(self.job_timeout_s, self.default_output_tokens)


def is_retryable(exc: BaseException) -> bool:
    """True if `exc` is a transient failure worth retrying (§6).

    Retryable: a timeout or a dropped connection (builtin `TimeoutError` —
    `asyncio.TimeoutError` aliases it — or `ConnectionError`, or an SDK
    `APIConnectionError`/`APITimeoutError` matched by name), or an HTTP status in
    {408, 409, 429} or any 5xx. Everything else — a 4xx auth/validation error, a
    bug like `KeyError` — is terminal: retrying only repeats the same failure.
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        return status in _RETRYABLE_STATUS or status >= 500
    return type(exc).__name__ in _RETRYABLE_EXC_NAMES


def backoff_delay(retry_index: int, policy: RetryPolicy, rand: float) -> float:
    """Seconds to wait before retry number `retry_index` (0 = the first retry).

    Exponential — `base_delay_s * 2**retry_index`, capped at `max_delay_s` — then
    *full jitter*: the capped ceiling is scaled by `rand`, a value in [0, 1) the
    caller draws (`random.random()`), so concurrent workers don't retry in
    lockstep and re-hammer a recovering provider. Pure: the randomness is
    injected, exactly as the clock is in ratelimit/cost.
    """
    if retry_index < 0:
        raise ValueError("retry_index must be non-negative")
    ceiling = min(policy.max_delay_s, policy.base_delay_s * 2.0**retry_index)
    return ceiling * rand
