"""Unit tests for retry policy, error classification, and backoff (ARCHITECTURE.md §6).

Everything under test is pure: the policy types validate their numbers,
`is_retryable` classifies a synthetic exception (no SDK needed — it duck-types on
`status_code` / class name), and `backoff_delay` is deterministic with the random
draw injected. Numbers are chosen so a swapped operator or boundary shifts the
result, pinning the arithmetic for the mutation gate.
"""

import asyncio

import pytest

from llmbus.ratelimit import ProviderLimits, RateLimiter
from llmbus.retry import (
    RetryPolicy,
    WorkerPolicy,
    backoff_delay,
    is_retryable,
    retry_budget_seconds,
)

# --- RetryPolicy validation --------------------------------------------------


def test_retry_policy_accepts_valid_numbers():
    policy = RetryPolicy(max_attempts=4, base_delay_s=0.5, max_delay_s=30)
    assert (policy.max_attempts, policy.base_delay_s, policy.max_delay_s) == (4, 0.5, 30)


def test_retry_policy_allows_single_attempt_no_retry():
    # max_attempts counts the first call, so 1 is valid (no retry). The check is
    # `< 1`, not `<= 1` — a mutated boundary would reject this.
    assert RetryPolicy(max_attempts=1, base_delay_s=0.5, max_delay_s=30).max_attempts == 1


def test_retry_policy_allows_equal_base_and_max_delay():
    # max == base is valid (fixed backoff); the check is `<`, not `<=`.
    assert RetryPolicy(max_attempts=2, base_delay_s=5, max_delay_s=5).max_delay_s == 5


@pytest.mark.parametrize("max_attempts", [0, -1])
def test_retry_policy_rejects_non_positive_attempts(max_attempts):
    with pytest.raises(ValueError, match=r"^max_attempts must be at least 1$"):
        RetryPolicy(max_attempts=max_attempts, base_delay_s=0.5, max_delay_s=30)


@pytest.mark.parametrize("base_delay_s", [0, -0.1])
def test_retry_policy_rejects_non_positive_base_delay(base_delay_s):
    with pytest.raises(ValueError, match=r"^base_delay_s must be positive$"):
        RetryPolicy(max_attempts=4, base_delay_s=base_delay_s, max_delay_s=30)


def test_retry_policy_rejects_max_below_base():
    with pytest.raises(ValueError, match=r"^max_delay_s must be at least base_delay_s$"):
        RetryPolicy(max_attempts=4, base_delay_s=10, max_delay_s=9)


def test_retry_policy_is_frozen():
    policy = RetryPolicy(max_attempts=4, base_delay_s=0.5, max_delay_s=30)
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError is dataclass-internal
        policy.max_attempts = 1


# --- WorkerPolicy validation -------------------------------------------------


def _retry():
    return RetryPolicy(max_attempts=4, base_delay_s=0.5, max_delay_s=30)


def test_worker_policy_accepts_valid_numbers():
    policy = WorkerPolicy(retry=_retry(), job_timeout_s=60, default_output_tokens=512)
    assert (policy.job_timeout_s, policy.default_output_tokens) == (60, 512)


def test_worker_policy_accepts_a_sub_second_timeout():
    # A fractional positive timeout is valid: the check is `<= 0`, not `<= 1`.
    assert (
        WorkerPolicy(retry=_retry(), job_timeout_s=0.5, default_output_tokens=512).job_timeout_s
        == 0.5
    )


def test_worker_policy_allows_zero_default_output_tokens():
    # The reserve default may be 0 (reserve nothing for output); the check is
    # `< 0`, not `<= 0`.
    assert (
        WorkerPolicy(
            retry=_retry(), job_timeout_s=60, default_output_tokens=0
        ).default_output_tokens
        == 0
    )


@pytest.mark.parametrize("job_timeout_s", [0, -1])
def test_worker_policy_rejects_non_positive_timeout(job_timeout_s):
    with pytest.raises(ValueError, match=r"^job_timeout_s must be positive$"):
        WorkerPolicy(retry=_retry(), job_timeout_s=job_timeout_s, default_output_tokens=512)


def test_worker_policy_rejects_negative_default_output_tokens():
    with pytest.raises(ValueError, match=r"^default_output_tokens must be non-negative$"):
        WorkerPolicy(retry=_retry(), job_timeout_s=60, default_output_tokens=-1)


def test_worker_policy_is_frozen():
    policy = WorkerPolicy(retry=_retry(), job_timeout_s=60, default_output_tokens=512)
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError is dataclass-internal
        policy.job_timeout_s = 1


# --- is_retryable ------------------------------------------------------------


class _StatusError(Exception):
    """An exception carrying an HTTP `status_code`, like the SDKs' APIStatusError."""

    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


class APIConnectionError(Exception):
    """Name-matched transient transport error (no status_code) — SDK-shaped."""


class APITimeoutError(Exception):
    """Name-matched transient timeout error (no status_code) — SDK-shaped."""


class _NonIntStatusError(Exception):
    status_code = "429"  # a string, not an int — must not be read as a status


def test_is_retryable_true_for_builtin_timeout():
    assert is_retryable(TimeoutError("slow")) is True


def test_is_retryable_true_for_asyncio_timeout():
    # asyncio.TimeoutError is an alias of TimeoutError since 3.11 — same branch.
    assert is_retryable(asyncio.TimeoutError()) is True


def test_is_retryable_true_for_connection_error():
    assert is_retryable(ConnectionError("reset")) is True


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 599])
def test_is_retryable_true_for_transient_statuses(status):
    assert is_retryable(_StatusError(status)) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422, 499])
def test_is_retryable_false_for_terminal_statuses(status):
    assert is_retryable(_StatusError(status)) is False


def test_is_retryable_true_for_named_connection_errors():
    assert is_retryable(APIConnectionError("dropped")) is True
    assert is_retryable(APITimeoutError("timed out")) is True


def test_is_retryable_false_for_unrelated_exception():
    assert is_retryable(ValueError("bad input")) is False
    assert is_retryable(KeyError("missing")) is False


def test_is_retryable_ignores_non_int_status_code():
    # A string status_code is not an int, so it falls through to the name check,
    # which this class does not match → terminal.
    assert is_retryable(_NonIntStatusError()) is False


# --- backoff_delay -----------------------------------------------------------


def _backoff_policy():
    # base 0.5s, cap 30s: doubling gives 0.5, 1, 2, 4, 8, 16, 32→cap 30.
    return RetryPolicy(max_attempts=10, base_delay_s=0.5, max_delay_s=30)


@pytest.mark.parametrize(
    ("retry_index", "expected_ceiling"),
    [(0, 0.5), (1, 1.0), (2, 2.0), (3, 4.0), (4, 8.0), (5, 16.0)],
)
def test_backoff_doubles_from_base_at_full_jitter(retry_index, expected_ceiling):
    # rand=1.0 is the jitter ceiling: base * 2**index, uncapped below index 6.
    assert backoff_delay(retry_index, _backoff_policy(), rand=1.0) == expected_ceiling


def test_backoff_is_capped_at_max_delay():
    # index 6 → 0.5 * 64 = 32, above the 30s cap → clamped to exactly 30.
    assert backoff_delay(6, _backoff_policy(), rand=1.0) == 30.0
    assert backoff_delay(20, _backoff_policy(), rand=1.0) == 30.0


def test_backoff_scales_the_ceiling_by_the_random_draw():
    # Full jitter: the ceiling is multiplied by the injected rand in [0, 1).
    assert backoff_delay(2, _backoff_policy(), rand=0.5) == 1.0  # 2.0 * 0.5
    assert backoff_delay(2, _backoff_policy(), rand=0.0) == 0.0  # 2.0 * 0.0


def test_backoff_rejects_negative_index():
    with pytest.raises(ValueError, match=r"^retry_index must be non-negative$"):
        backoff_delay(-1, _backoff_policy(), rand=1.0)


# --- retry_budget_seconds (§14 #21) --------------------------------------------
#
# This is the number a polling producer sizes its wait against, so the
# arithmetic is pinned exactly rather than by inequality: every case below is a
# closed-form value, and each targets a distinct way the formula could be wrong.


def _worker(max_attempts, job_timeout_s, base_delay_s=0.5, max_delay_s=30.0):
    return WorkerPolicy(
        retry=RetryPolicy(
            max_attempts=max_attempts, base_delay_s=base_delay_s, max_delay_s=max_delay_s
        ),
        job_timeout_s=job_timeout_s,
        default_output_tokens=512,
    )


def test_single_attempt_has_no_backoff_at_all():
    # max_attempts=1 means no retry, so the bound is exactly one timeout. This
    # also pins the retry COUNT: an off-by-one would add a spurious backoff.
    assert retry_budget_seconds(_worker(max_attempts=1, job_timeout_s=30)) == 30.0


def test_stock_worker_bound_is_exact():
    # 4 x 60s of attempts + backoffs 0.5 + 1 + 2 (three retries) = 243.5.
    assert retry_budget_seconds(_worker(max_attempts=4, job_timeout_s=60)) == 243.5


def test_tuned_worker_bound_is_exact():
    # The deployed pilot budget: 2 x 30s + a single 0.5s backoff.
    assert retry_budget_seconds(_worker(max_attempts=2, job_timeout_s=30)) == 60.5


def test_backoff_doubles_per_retry():
    # base 1s, cap high enough never to bind: 1 + 2 + 4 = 7 over three retries.
    # Pins the exponential growth; a linear or constant backoff misses this.
    policy = _worker(max_attempts=4, job_timeout_s=10, base_delay_s=1.0, max_delay_s=1000.0)
    assert retry_budget_seconds(policy) == 40.0 + 7.0


def test_backoff_is_capped_per_retry_not_in_total():
    # base 10, cap 15, three retries: 10 + 15 + 15 = 40, NOT 10 + 20 + 40 = 70
    # (uncapped) and NOT 15 (a total cap). Kills min/max confusion.
    policy = _worker(max_attempts=4, job_timeout_s=1, base_delay_s=10.0, max_delay_s=15.0)
    assert retry_budget_seconds(policy) == 4.0 + 40.0


def test_attempts_multiply_the_timeout():
    # Every attempt can burn the full per-attempt timeout, so the timeout term
    # scales with attempts rather than being counted once.
    one = retry_budget_seconds(_worker(max_attempts=1, job_timeout_s=7))
    three = retry_budget_seconds(_worker(max_attempts=3, job_timeout_s=7))
    assert one == 7.0
    assert three == 21.0 + 1.5  # 3 x 7 + backoffs 0.5 + 1


def test_bound_covers_the_real_jittered_backoff():
    # Jitter only ever SHORTENS a wait (backoff_delay scales by rand in [0, 1)),
    # so summing the un-jittered ceilings is a true upper bound. Compare against
    # the worst the actual backoff function can produce, rand=1.0.
    policy = _worker(max_attempts=5, job_timeout_s=2)
    realised = sum(backoff_delay(i, policy.retry, rand=1.0) for i in range(4))
    assert retry_budget_seconds(policy) == 5 * 2 + realised


def test_bound_matches_backoff_at_and_beyond_the_cap_boundary():
    policy = _worker(
        max_attempts=5,
        job_timeout_s=3,
        base_delay_s=2.0,
        max_delay_s=8.0,
    )

    assert [backoff_delay(i, policy.retry, rand=1.0) for i in range(4)] == [
        2.0,
        4.0,
        8.0,
        8.0,
    ]
    assert retry_budget_seconds(policy) == 5 * 3 + 2 + 4 + 8 + 8


async def test_retry_budget_deliberately_excludes_the_rate_limit_wait():
    """The rate-limit wait is REAL and is NOT in the budget — pinned on purpose.

    Evidence (from review): processing.py awaits RateLimiter.acquire before every
    attempt, and a drained bucket sleeps out the window. So actual occupancy can
    exceed retry_budget_seconds by an amount that has no static ceiling — it
    depends on the bucket, which depends on every other job.

    This asserts the SHORTFALL rather than a bound, so nobody re-reads the name
    as a guarantee. Cost safety is Job.ttl_s (§14 #22): the worker refuses an
    expired job, which needs no prediction of queueing at all.
    """
    sleeps: list[float] = []

    async def record_sleep(delay: float) -> None:
        sleeps.append(delay)

    limiter = RateLimiter(
        {"openai": ProviderLimits(requests_per_min=60, tokens_per_min=60)},
        clock=lambda: 0.0,
        sleep=record_sleep,
    )
    await limiter.acquire("openai", 60)
    await limiter.acquire("openai", 60)
    policy = _worker(max_attempts=1, job_timeout_s=1)

    assert sleeps == [60.0]
    real_occupancy = sleeps[0] + policy.job_timeout_s
    assert retry_budget_seconds(policy) == 1.0
    assert retry_budget_seconds(policy) < real_occupancy
