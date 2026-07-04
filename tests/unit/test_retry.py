"""Unit tests for retry policy, error classification, and backoff (ARCHITECTURE.md §6).

Everything under test is pure: the policy types validate their numbers,
`is_retryable` classifies a synthetic exception (no SDK needed — it duck-types on
`status_code` / class name), and `backoff_delay` is deterministic with the random
draw injected. Numbers are chosen so a swapped operator or boundary shifts the
result, pinning the arithmetic for the mutation gate.
"""

import asyncio

import pytest

from llmbus.retry import (
    RetryPolicy,
    WorkerPolicy,
    backoff_delay,
    is_retryable,
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
