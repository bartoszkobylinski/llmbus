"""Unit tests for worker-policy config parsing (§6, §10).

`parse_worker_policy` is parsed separately from `Config` (worker-only keys), pure
over an injected dict. These pin the required keys, the int/float validation, and
the cross-field `max >= base` rule re-raised as `ConfigError`.
"""

import pytest

from llmbus.config import ConfigError, parse_connect_policy, parse_worker_policy

# A complete, valid worker-policy environment; tests drop/override from it.
_ENV = {
    "WORKER_MAX_ATTEMPTS": "4",
    "WORKER_BACKOFF_BASE_S": "0.5",
    "WORKER_BACKOFF_MAX_S": "30",
    "WORKER_JOB_TIMEOUT_S": "60",
    "WORKER_DEFAULT_OUTPUT_TOKENS": "512",
}


def _env(**overrides):
    env = dict(_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def test_parses_a_valid_policy():
    policy = parse_worker_policy(_env())
    assert policy.retry.max_attempts == 4
    assert policy.retry.base_delay_s == 0.5
    assert policy.retry.max_delay_s == 30
    assert policy.job_timeout_s == 60
    assert policy.default_output_tokens == 512


@pytest.mark.parametrize("key", sorted(_ENV))
def test_missing_key_raises_config_error(key):
    with pytest.raises(ConfigError, match=rf"^missing required setting {key}$"):
        parse_worker_policy(_env(**{key: None}))


def test_non_integer_attempts_rejected():
    with pytest.raises(
        ConfigError, match=r"^setting WORKER_MAX_ATTEMPTS must be an integer, got '4.5'$"
    ):
        parse_worker_policy(_env(WORKER_MAX_ATTEMPTS="4.5"))


@pytest.mark.parametrize("value", ["0", "-1"])
def test_non_positive_attempts_rejected(value):
    with pytest.raises(ConfigError, match=r"must be a positive integer"):
        parse_worker_policy(_env(WORKER_MAX_ATTEMPTS=value))


def test_non_positive_default_output_tokens_rejected():
    with pytest.raises(ConfigError, match=r"must be a positive integer"):
        parse_worker_policy(_env(WORKER_DEFAULT_OUTPUT_TOKENS="0"))


def test_non_number_backoff_rejected():
    with pytest.raises(
        ConfigError, match=r"^setting WORKER_BACKOFF_BASE_S must be a number, got 'soon'$"
    ):
        parse_worker_policy(_env(WORKER_BACKOFF_BASE_S="soon"))


def test_non_positive_timeout_rejected():
    with pytest.raises(ConfigError, match=r"must be a positive finite number"):
        parse_worker_policy(_env(WORKER_JOB_TIMEOUT_S="0"))


def test_max_below_base_reraised_as_config_error():
    # RetryPolicy's cross-field check surfaces as ConfigError so a bad .env fails
    # uniformly (not as a bare ValueError).
    with pytest.raises(ConfigError, match=r"^max_delay_s must be at least base_delay_s$"):
        parse_worker_policy(_env(WORKER_BACKOFF_BASE_S="10", WORKER_BACKOFF_MAX_S="9"))


# --- parse_connect_policy (broker handshake, §14 #16) ------------------------
#
# Separate keys from the job retry above, on purpose: a cold broker wants many
# short attempts, a paid model call few long ones. Same validation contract.

_CONNECT_ENV = {
    "WORKER_CONNECT_MAX_ATTEMPTS": "10",
    "WORKER_CONNECT_BACKOFF_BASE_S": "0.25",
    "WORKER_CONNECT_BACKOFF_MAX_S": "5",
}


def _connect_env(**overrides):
    env = dict(_CONNECT_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


def test_parses_a_valid_connect_policy():
    policy = parse_connect_policy(_connect_env())
    assert policy.max_attempts == 10
    assert policy.base_delay_s == 0.25
    assert policy.max_delay_s == 5


@pytest.mark.parametrize("key", sorted(_CONNECT_ENV))
def test_missing_connect_key_raises_config_error(key):
    with pytest.raises(ConfigError, match=rf"^missing required setting {key}$"):
        parse_connect_policy(_connect_env(**{key: None}))


def test_connect_policy_ignores_the_job_retry_keys():
    # The two policies must not be coupled: a job-retry env alone is not a valid
    # connect env, and vice versa. Pins that no one "helpfully" falls back.
    with pytest.raises(
        ConfigError, match=r"^missing required setting WORKER_CONNECT_MAX_ATTEMPTS$"
    ):
        parse_connect_policy(_ENV)


def test_non_integer_connect_attempts_rejected():
    with pytest.raises(
        ConfigError,
        match=r"^setting WORKER_CONNECT_MAX_ATTEMPTS must be an integer, got 'lots'$",
    ):
        parse_connect_policy(_connect_env(WORKER_CONNECT_MAX_ATTEMPTS="lots"))


@pytest.mark.parametrize("value", ["0", "-1"])
def test_non_positive_connect_attempts_rejected(value):
    with pytest.raises(ConfigError, match=r"must be a positive integer"):
        parse_connect_policy(_connect_env(WORKER_CONNECT_MAX_ATTEMPTS=value))


def test_non_positive_connect_backoff_rejected():
    with pytest.raises(ConfigError, match=r"must be a positive finite number"):
        parse_connect_policy(_connect_env(WORKER_CONNECT_BACKOFF_BASE_S="0"))


@pytest.mark.parametrize("value", ["nan", "inf"])
def test_non_finite_connect_backoff_rejected(value):
    with pytest.raises(ConfigError, match=r"must be a positive finite number"):
        parse_connect_policy(_connect_env(WORKER_CONNECT_BACKOFF_BASE_S=value))


def test_connect_max_below_base_reraised_as_config_error():
    with pytest.raises(ConfigError, match=r"^max_delay_s must be at least base_delay_s$"):
        parse_connect_policy(
            _connect_env(WORKER_CONNECT_BACKOFF_BASE_S="10", WORKER_CONNECT_BACKOFF_MAX_S="9")
        )
