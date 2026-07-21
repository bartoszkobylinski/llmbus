"""Unit tests for the worker's job-processing core (ARCHITECTURE.md §6).

`process_job` orchestrates I/O, but every collaborator is injected — a real
in-memory `Store`, fake provider/rate-limiter/callback, and recording sleep — so
the whole reliability path (route, rate-limit, retry/backoff, cost, one-shot
finalize, best-effort callback) runs with no network, no live server, and no real
time. That is exactly what lets this module sit in the mutation gate.
"""

import asyncio
import hashlib
import hmac
from datetime import datetime, timezone

import pytest

from llmbus.processing import (
    CALLBACK_SIGNATURE_HEADER,
    WorkerDeps,
    _default_apply_timeout,
    callback_headers,
    callback_signature,
    estimate_tokens,
    is_expired,
    process_job,
    result_error,
    result_ok,
)
from llmbus.providers.base import ProviderResult
from llmbus.retry import RetryPolicy, WorkerPolicy
from llmbus.schema import Job, JobParams, Message, Result, Usage
from llmbus.store import Store

# --- fakes -------------------------------------------------------------------


class FakeProvider:
    """A `Provider` whose `call` replays a scripted list of outcomes — a
    `ProviderResult` to return or an exception to raise, one per call."""

    def __init__(self, name, outcomes):
        self.name = name
        self._outcomes = list(outcomes)
        self.calls = []

    async def call(self, model, messages, params):
        self.calls.append((model, list(messages), params))
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


class FakeRateLimiter:
    """Records every `acquire(provider, tokens)` instead of throttling."""

    def __init__(self):
        self.acquired = []

    async def acquire(self, provider, tokens):
        self.acquired.append((provider, tokens))


class FakeCallback:
    """Records delivered payloads; optionally raises to exercise the swallow path."""

    def __init__(self, fail=False):
        self.deliveries = []
        self._fail = fail

    async def __call__(self, url, payload):
        self.deliveries.append((url, payload))
        if self._fail:
            raise RuntimeError("callback endpoint down")


class RecordingSleep:
    """Stands in for the backoff sleep; records durations without waiting."""

    def __init__(self):
        self.calls = []

    async def __call__(self, seconds):
        self.calls.append(seconds)


async def _passthrough(call, timeout_s):
    """apply_timeout that just awaits the call — no real timeout in logic tests."""
    return await call


class _StatusError(Exception):
    """Carries an HTTP `status_code`, like the SDKs' APIStatusError."""

    def __init__(self, status_code):
        super().__init__(f"status {status_code}")
        self.status_code = status_code


# --- builders ----------------------------------------------------------------


def make_policy(*, max_attempts=4, timeout=60, default_out=512):
    return WorkerPolicy(
        retry=RetryPolicy(max_attempts=max_attempts, base_delay_s=0.5, max_delay_s=30),
        job_timeout_s=timeout,
        default_output_tokens=default_out,
    )


def make_job(**overrides):
    """A valid classify job; override `content`/`max_tokens` (which feed the
    messages/params) or any Job field (model, callback_url, meta) via kwargs."""
    content = overrides.pop("content", "hello world")
    max_tokens = overrides.pop("max_tokens", 8)
    data = {
        "project": "hate-moderator",
        "kind": "classify",
        "model": "gpt-5-nano",
        "messages": [Message(role="user", content=content)],
        "params": JobParams(max_tokens=max_tokens),
        "meta": {},
    }
    data.update(overrides)
    return Job(**data)


def build_deps(store, provider, **overrides):
    """WorkerDeps with recording fakes; override any field via kwargs. Assert on
    the fakes through `deps.rate_limiter` / `deps.sleep` / `deps.deliver_callback`."""
    defaults = {
        "providers": {provider.name: provider},
        "rate_limiter": FakeRateLimiter(),
        "store": store,
        "policy": make_policy(),
        "deliver_callback": FakeCallback(),
        "sleep": RecordingSleep(),
        "rand": (lambda: 1.0),
        "apply_timeout": _passthrough,
    }
    defaults.update(overrides)
    return WorkerDeps(**defaults)


def ok_result(completion="done", input_tokens=10, output_tokens=20):
    return ProviderResult(
        completion=completion,
        usage=Usage(input_tokens=input_tokens, output_tokens=output_tokens),
    )


# --- estimate_tokens ---------------------------------------------------------


def test_estimate_tokens_input_chars_over_four_plus_max_tokens():
    # "hello world" = 11 chars → 11 // 4 = 2 input tokens; + max_tokens 8 = 10.
    assert estimate_tokens(make_job(content="hello world", max_tokens=8), 512) == 10


def test_estimate_tokens_floors_the_char_division():
    # 10 chars // 4 = 2 (not 2.5): integer floor.
    job = make_job(content="aaaaaaaaaa", max_tokens=100)
    assert estimate_tokens(job, 512) == 102


def test_estimate_tokens_sums_across_messages():
    job = Job(
        project="p",
        kind="classify",
        model="gpt-5-nano",
        messages=[Message(role="system", content="aaaa"), Message(role="user", content="bbbbbb")],
        params=JobParams(max_tokens=100),
    )
    # (4 + 6) // 4 = 2, + 100 = 102.
    assert estimate_tokens(job, 512) == 102


def test_estimate_tokens_falls_back_to_default_output_when_max_tokens_unset():
    # "aaaaaaaa" = 8 chars → 2; max_tokens unset → default 512 → 514.
    assert estimate_tokens(make_job(content="aaaaaaaa", max_tokens=None), 512) == 514


def test_estimate_tokens_empty_messages_are_zero_input():
    job = Job(
        project="p",
        kind="classify",
        model="gpt-5-nano",
        messages=[],
        params=JobParams(max_tokens=1),
    )
    assert estimate_tokens(job, 512) == 1


# --- result builders ---------------------------------------------------------


def test_result_ok_folds_cost_and_echoes_meta():
    job = make_job(meta={"comment_id": "c1"})
    result = result_ok(
        job, "openai", ok_result(completion="hi", input_tokens=3, output_tokens=7), cost=0.5
    )
    assert result.status == "ok"
    assert result.completion == "hi"
    assert result.provider == "openai"
    assert result.job_id == job.job_id
    assert result.meta == {"comment_id": "c1"}
    assert (result.usage.input_tokens, result.usage.output_tokens) == (3, 7)
    assert result.usage.cost_usd == 0.5


def test_result_error_carries_message_and_no_usage():
    job = make_job(meta={"comment_id": "c2"})
    result = result_error(job, "anthropic", "boom")
    assert result.status == "error"
    assert result.error == "boom"
    assert result.provider == "anthropic"
    assert result.completion is None
    assert result.meta == {"comment_id": "c2"}
    assert (result.usage.input_tokens, result.usage.output_tokens, result.usage.cost_usd) == (
        0,
        0,
        0.0,
    )


def test_result_error_allows_no_provider():
    result = result_error(make_job(), None, "no route")
    assert result.provider is None


def test_callback_signature_is_hmac_sha256_of_the_exact_body():
    body = b'{"job_id":"abc","status":"ok"}'
    expected = hmac.new(b"shared-secret", body, hashlib.sha256).hexdigest()

    assert callback_signature("shared-secret", body) == f"sha256={expected}"


def test_callback_headers_include_content_type_and_framed_signature_when_secret_set():
    body = b'{"job_id":"abc","status":"ok"}'

    headers = callback_headers("shared-secret", body)

    assert headers["Content-Type"] == "application/json"
    assert headers[CALLBACK_SIGNATURE_HEADER] == callback_signature("shared-secret", body)


def test_callback_headers_omit_signature_when_secret_is_none():
    headers = callback_headers(None, b'{"job_id":"abc"}')

    assert headers == {"Content-Type": "application/json"}


# --- process_job: happy path -------------------------------------------------


async def test_process_job_success_finalizes_store_with_cost():
    async with Store(":memory:") as store:
        job = make_job(model="gpt-5-nano")  # 10 in, 20 out priced below
        await store.insert_pending(job)
        provider = FakeProvider("openai", [ok_result(input_tokens=10, output_tokens=20)])
        deps = build_deps(store, provider)

        result = await process_job(deps, job)

        assert result.status == "ok"
        assert result.completion == "done"
        assert result.provider == "openai"
        # The provider is called with the job's own model and params, untouched.
        assert provider.calls[0][0] == job.model
        assert provider.calls[0][2] == job.params
        stored = await store.get(job.job_id)
        assert stored.status == "ok"
        assert stored.is_terminal
        assert (stored.usage.input_tokens, stored.usage.output_tokens) == (10, 20)
        # gpt-5-nano: 10 * $0.05/Mtok + 20 * $0.40/Mtok = 8.5 / 1e6.
        assert stored.usage.cost_usd == pytest.approx(8.5e-6)


async def test_process_job_reserves_rate_limit_with_the_estimate():
    async with Store(":memory:") as store:
        job = make_job(content="hello world", max_tokens=8)  # estimate = 2 + 8 = 10
        await store.insert_pending(job)
        provider = FakeProvider("openai", [ok_result()])
        deps = build_deps(store, provider)

        await process_job(deps, job)

        assert deps.rate_limiter.acquired == [("openai", 10)]
        assert deps.sleep.calls == []  # no retry, no backoff


async def test_process_job_delivers_callback_on_success():
    async with Store(":memory:") as store:
        job = make_job(callback_url="http://cb/internal", meta={"comment_id": "z"})
        await store.insert_pending(job)
        provider = FakeProvider("openai", [ok_result(completion="ok!")])
        deps = build_deps(store, provider)

        await process_job(deps, job)

        assert len(deps.deliver_callback.deliveries) == 1
        url, payload = deps.deliver_callback.deliveries[0]
        assert url == "http://cb/internal"
        assert payload["status"] == "ok"
        assert payload["completion"] == "ok!"
        assert payload["meta"] == {"comment_id": "z"}
        assert payload["usage"]["in"] == 10  # by_alias wire form (§4)


async def test_process_job_no_callback_when_url_absent():
    async with Store(":memory:") as store:
        job = make_job(callback_url=None)
        await store.insert_pending(job)
        deps = build_deps(store, FakeProvider("openai", [ok_result()]))

        await process_job(deps, job)

        assert deps.deliver_callback.deliveries == []


async def test_process_job_applies_the_configured_per_attempt_timeout():
    captured = []

    async def capture_timeout(call, timeout_s):
        captured.append(timeout_s)
        return await call

    async with Store(":memory:") as store:
        job = make_job()
        await store.insert_pending(job)
        deps = build_deps(
            store,
            FakeProvider("openai", [ok_result()]),
            apply_timeout=capture_timeout,
            policy=make_policy(timeout=42),
        )

        await process_job(deps, job)

        assert captured == [42]  # the policy's job_timeout_s, not a default


async def test_process_job_reserves_default_output_tokens_when_max_tokens_unset():
    async with Store(":memory:") as store:
        job = make_job(content="hello world", max_tokens=None)  # 11 // 4 = 2 input
        await store.insert_pending(job)
        deps = build_deps(
            store,
            FakeProvider("openai", [ok_result()]),
            policy=make_policy(default_out=512),
        )

        await process_job(deps, job)

        # No max_tokens → output side falls back to default_output_tokens: 2 + 512.
        assert deps.rate_limiter.acquired == [("openai", 514)]


# --- process_job: retry / backoff --------------------------------------------


async def test_process_job_retries_then_succeeds():
    async with Store(":memory:") as store:
        job = make_job()
        await store.insert_pending(job)
        provider = FakeProvider("openai", [_StatusError(429), ok_result(completion="recovered")])
        deps = build_deps(store, provider)

        result = await process_job(deps, job)

        assert result.status == "ok"
        assert result.completion == "recovered"
        assert len(provider.calls) == 2
        assert len(deps.rate_limiter.acquired) == 2  # re-reserved before the retry
        assert deps.sleep.calls == [0.5]  # backoff_delay(0) at base 0.5, rand 1.0


async def test_process_job_retries_a_timeout():
    async with Store(":memory:") as store:
        job = make_job()
        await store.insert_pending(job)
        provider = FakeProvider("openai", [TimeoutError("slow"), ok_result()])
        deps = build_deps(store, provider)

        result = await process_job(deps, job)

        assert result.status == "ok"
        assert len(provider.calls) == 2


async def test_process_job_single_attempt_policy_does_not_retry_retryable_error():
    async with Store(":memory:") as store:
        job = make_job()
        await store.insert_pending(job)
        provider = FakeProvider("openai", [_StatusError(429), ok_result(completion="unused")])
        deps = build_deps(store, provider, policy=make_policy(max_attempts=1))

        result = await process_job(deps, job)

        assert result.status == "error"
        assert result.error == "_StatusError: status 429"
        assert len(provider.calls) == 1
        assert deps.rate_limiter.acquired == [("openai", estimate_tokens(job, 512))]
        assert deps.sleep.calls == []


async def test_process_job_uses_fresh_jitter_and_retry_index_for_each_backoff():
    draws = iter([0.25, 0.75])

    async with Store(":memory:") as store:
        job = make_job()
        await store.insert_pending(job)
        provider = FakeProvider(
            "openai",
            [_StatusError(500), _StatusError(503), ok_result(completion="recovered")],
        )
        deps = build_deps(
            store,
            provider,
            policy=WorkerPolicy(
                retry=RetryPolicy(max_attempts=3, base_delay_s=2, max_delay_s=10),
                job_timeout_s=60,
                default_output_tokens=512,
            ),
            rand=lambda: next(draws),
        )

        result = await process_job(deps, job)

        assert result.status == "ok"
        # Retry 0: ceiling 2 * 0.25 = 0.5. Retry 1: ceiling 4 * 0.75 = 3.0.
        assert deps.sleep.calls == [0.5, 3.0]
        assert len(deps.rate_limiter.acquired) == 3


async def test_process_job_gives_up_after_max_attempts():
    async with Store(":memory:") as store:
        job = make_job()
        await store.insert_pending(job)
        provider = FakeProvider("openai", [_StatusError(500), _StatusError(500), _StatusError(503)])
        deps = build_deps(store, provider, policy=make_policy(max_attempts=3))

        result = await process_job(deps, job)

        assert result.status == "error"
        assert result.error == "_StatusError: status 503"  # the last failure
        assert len(provider.calls) == 3
        assert len(deps.rate_limiter.acquired) == 3
        assert deps.sleep.calls == [0.5, 1.0]  # backoff after attempts 0 and 1
        stored = await store.get(job.job_id)
        assert stored.status == "error"


async def test_process_job_does_not_retry_a_terminal_error():
    async with Store(":memory:") as store:
        job = make_job()
        await store.insert_pending(job)
        provider = FakeProvider("openai", [_StatusError(400)])  # 4xx → terminal
        deps = build_deps(store, provider)

        result = await process_job(deps, job)

        assert result.status == "error"
        assert result.error == "_StatusError: status 400"
        assert result.provider == "openai"  # the routed provider, on the error too
        assert len(provider.calls) == 1  # no retry
        assert deps.sleep.calls == []


async def test_process_job_fail_loud_truncation_error_is_terminal_and_preserved():
    async with Store(":memory:") as store:
        job = make_job()
        await store.insert_pending(job)
        provider = FakeProvider(
            "openai",
            [
                ValueError(
                    "OpenAI response finished with finish_reason='length', not 'stop' — "
                    "the completion is truncated or absent; with 'length' the "
                    "max_completion_tokens budget ran out (GPT-5 spends it on reasoning "
                    "tokens too, so raise params.max_tokens well above the expected output)"
                )
            ],
        )
        deps = build_deps(store, provider)

        result = await process_job(deps, job)

        assert result.status == "error"
        assert result.error == (
            "ValueError: OpenAI response finished with finish_reason='length', not "
            "'stop' — the completion is truncated or absent; with 'length' the "
            "max_completion_tokens budget ran out (GPT-5 spends it on reasoning "
            "tokens too, so raise params.max_tokens well above the expected output)"
        )
        assert result.provider == "openai"
        assert len(provider.calls) == 1
        assert deps.sleep.calls == []


async def test_process_job_error_still_delivers_callback():
    async with Store(":memory:") as store:
        job = make_job(callback_url="http://cb")
        await store.insert_pending(job)
        provider = FakeProvider("openai", [ValueError("bad prompt")])  # terminal
        deps = build_deps(store, provider)

        await process_job(deps, job)

        assert len(deps.deliver_callback.deliveries) == 1
        assert deps.deliver_callback.deliveries[0][1]["status"] == "error"


async def test_process_job_error_callback_payload_uses_usage_aliases():
    async with Store(":memory:") as store:
        job = make_job(callback_url="http://cb")
        await store.insert_pending(job)
        provider = FakeProvider("openai", [ValueError("bad prompt")])
        deps = build_deps(store, provider)

        await process_job(deps, job)

        payload = deps.deliver_callback.deliveries[0][1]
        assert payload["usage"] == {"in": 0, "out": 0, "cost_usd": 0.0}
        assert "input_tokens" not in payload["usage"]
        assert "output_tokens" not in payload["usage"]


# --- process_job: routing errors ---------------------------------------------


async def test_process_job_unknown_model_errors_without_calling_a_provider():
    async with Store(":memory:") as store:
        job = make_job(model="no-such-model")
        await store.insert_pending(job)
        provider = FakeProvider("openai", [])
        deps = build_deps(store, provider)

        result = await process_job(deps, job)

        assert result.status == "error"
        assert result.error == "no provider serves model 'no-such-model'"
        assert result.provider is None
        assert provider.calls == []
        assert deps.rate_limiter.acquired == []  # never reached the rate limit


async def test_process_job_missing_adapter_errors():
    async with Store(":memory:") as store:
        job = make_job(model="claude-haiku-4-5")  # routes to "anthropic"
        await store.insert_pending(job)
        # Registry has only openai — the anthropic adapter is unwired.
        deps = build_deps(store, FakeProvider("openai", []))

        result = await process_job(deps, job)

        assert result.status == "error"
        assert result.error == "no adapter configured for provider 'anthropic'"
        assert result.provider == "anthropic"


# --- process_job: idempotency & callback robustness --------------------------


async def test_process_job_redelivery_does_not_double_finalize_or_callback():
    async with Store(":memory:") as store:
        job = make_job(callback_url="http://cb")
        await store.insert_pending(job)
        provider = FakeProvider(
            "openai", [ok_result(completion="first"), ok_result(completion="second")]
        )
        deps = build_deps(store, provider)

        first = await process_job(deps, job)
        second = await process_job(deps, job)  # at-least-once redelivery

        assert first.completion == "first"
        assert second.completion == "second"  # the model did re-run (at-least-once)
        assert len(provider.calls) == 2
        # ...but finalize is one-shot: the store keeps the FIRST result and the
        # callback fires exactly once (§6).
        stored = await store.get(job.job_id)
        assert stored.completion == "first"
        assert len(deps.deliver_callback.deliveries) == 1


async def test_process_job_redelivered_terminal_row_suppresses_error_callback_too():
    async with Store(":memory:") as store:
        job = make_job(callback_url="http://cb")
        await store.insert_pending(job)
        await store.finalize(Result(job_id=job.job_id, status="ok", completion="already done"))
        provider = FakeProvider("openai", [ValueError("redelivery failed")])
        deps = build_deps(store, provider)

        result = await process_job(deps, job)

        assert result.status == "error"
        assert len(provider.calls) == 1
        assert deps.deliver_callback.deliveries == []
        stored = await store.get(job.job_id)
        assert stored.status == "ok"
        assert stored.completion == "already done"


async def test_process_job_swallows_callback_failure(caplog):
    async with Store(":memory:") as store:
        job = make_job(callback_url="http://cb")
        await store.insert_pending(job)
        provider = FakeProvider("openai", [ok_result()])
        deps = build_deps(store, provider, deliver_callback=FakeCallback(fail=True))

        result = await process_job(deps, job)  # must not raise

        assert result.status == "ok"
        stored = await store.get(job.job_id)
        assert stored.status == "ok"  # result still durably stored
        # Exact rendered message pins the URL, job_id, and the exception text into
        # the warning (a dropped field or wrapped format string would fail here).
        expected = f"callback POST to http://cb failed for job {job.job_id}: callback endpoint down"
        assert expected in caplog.messages


async def test_process_job_prices_using_job_submitted_at_date_not_today():
    async with Store(":memory:") as store:
        job = make_job(
            model="claude-sonnet-5",
            submitted_at=datetime(2026, 8, 31, 23, 59, tzinfo=timezone.utc),
        )
        await store.insert_pending(job)
        provider = FakeProvider(
            "anthropic", [ok_result(completion="priced", input_tokens=1_000_000, output_tokens=0)]
        )
        deps = build_deps(store, provider)

        result = await process_job(deps, job)

        assert result.status == "ok"
        assert result.usage.cost_usd == 2.0
        stored = await store.get(job.job_id)
        assert stored.usage.cost_usd == 2.0


# --- _default_apply_timeout --------------------------------------------------


async def test_default_apply_timeout_returns_a_fast_result():
    sentinel = ok_result(completion="fast")

    async def quick():
        return sentinel

    assert await _default_apply_timeout(quick(), 5) is sentinel


async def test_default_apply_timeout_raises_on_a_slow_call():
    async def slow():
        await asyncio.sleep(1)
        return ok_result()

    with pytest.raises(asyncio.TimeoutError):
        await _default_apply_timeout(slow(), 0.01)


# --- deadlines: Job.ttl_s (§14 #22) ------------------------------------------
#
# The producer states how long its work stays wanted; the worker refuses an
# expired job instead of calling the provider. This is what makes cost safety
# independent of any queueing prediction: with one serial partition and several
# independent producers, no producer can bound its own wait from local
# information, so the only reliable protection is not paying for work that has
# already been abandoned.

_T0 = datetime(2026, 7, 22, 12, 0, 0, tzinfo=timezone.utc)


def _at(seconds):
    from datetime import timedelta

    return _T0 + timedelta(seconds=seconds)


def test_a_job_without_a_deadline_never_expires():
    # Batch producers collect results whenever; they must not be dropped.
    assert is_expired(make_job(submitted_at=_T0, ttl_s=None), _at(10_000)) is False


def test_a_job_inside_its_deadline_is_live():
    assert is_expired(make_job(submitted_at=_T0, ttl_s=60), _at(59.9)) is False


def test_the_deadline_is_inclusive_at_the_boundary():
    # Exactly at the deadline counts as expired: the producer has stopped waiting.
    assert is_expired(make_job(submitted_at=_T0, ttl_s=60), _at(60)) is True


def test_a_job_past_its_deadline_is_expired():
    assert is_expired(make_job(submitted_at=_T0, ttl_s=60), _at(60.1)) is True


async def test_an_expired_job_is_never_sent_to_the_provider(tmp_path):
    """The whole point: no provider call, so no charge for abandoned work."""
    store = Store(str(tmp_path / "s.db"))
    await store.connect()
    provider = FakeProvider("openai", [ok_result()])
    job = make_job(submitted_at=_T0, ttl_s=30)
    await store.insert_pending(job)
    result = await process_job(
        build_deps(store, provider, now=lambda: _at(31)),
        job,
    )
    assert provider.calls == []
    assert result.status == "error"
    # Attributed to the provider it WOULD have gone to — an expired job is a
    # dropped provider call, not a routing failure, and the ledger should say so.
    assert result.provider == "openai"
    assert result.error == (
        "expired before attempt 1: the producer's 30.0s deadline elapsed while this "
        "job was queued, so it was dropped unrun rather than billed"
    )
    await store.close()


async def test_an_expired_job_does_not_reserve_rate_limit_budget(tmp_path):
    """Checked BEFORE acquire too: under a backlog most queued jobs may be
    expired, and reserving quota for them would starve the ones still wanted."""
    store = Store(str(tmp_path / "s.db"))
    await store.connect()
    provider = FakeProvider("openai", [ok_result()])
    job = make_job(submitted_at=_T0, ttl_s=30)
    await store.insert_pending(job)
    deps = build_deps(store, provider, now=lambda: _at(31))
    await process_job(deps, job)
    assert deps.rate_limiter.acquired == []
    await store.close()


async def test_an_expired_job_still_reaches_a_terminal_stored_state(tmp_path):
    """It must not be silently dropped: a producer still polling would otherwise
    wait out its own timeout, and the pending count (§11) would drift up forever."""
    store = Store(str(tmp_path / "s.db"))
    await store.connect()
    job = make_job(submitted_at=_T0, ttl_s=30)
    await store.insert_pending(job)
    await process_job(
        build_deps(store, FakeProvider("openai", [ok_result()]), now=lambda: _at(31)), job
    )
    stored = await store.get(job.job_id)
    assert stored is not None
    assert stored.is_terminal
    assert stored.status == "error"
    await store.close()


async def test_a_live_job_is_processed_normally(tmp_path):
    store = Store(str(tmp_path / "s.db"))
    await store.connect()
    provider = FakeProvider("openai", [ok_result()])
    job = make_job(submitted_at=_T0, ttl_s=300)
    await store.insert_pending(job)
    result = await process_job(build_deps(store, provider, now=lambda: _at(5)), job)
    assert len(provider.calls) == 1
    assert result.status == "ok"
    await store.close()


async def test_a_job_that_expires_while_waiting_on_the_rate_limiter_is_dropped(tmp_path):
    """The second check earns its place here. acquire() can sleep out a whole
    window, and that wait is exactly what retry_budget_seconds cannot bound — so
    the deadline has to be re-read after it, immediately before spending money."""
    store = Store(str(tmp_path / "s.db"))
    await store.connect()
    provider = FakeProvider("openai", [ok_result()])
    job = make_job(submitted_at=_T0, ttl_s=30)
    await store.insert_pending(job)

    clock = iter([_at(1), _at(31)])  # live at the first check, expired after acquire

    result = await process_job(
        build_deps(store, provider, now=lambda: next(clock)),
        job,
    )
    assert provider.calls == []
    assert result.status == "error"
    assert result.provider == "openai"
    assert result.error == (
        "expired before attempt 1: the producer's 30.0s deadline elapsed while this "
        "job was queued, so it was dropped unrun rather than billed"
    )
    await store.close()


def test_the_default_clock_is_timezone_aware_utc():
    """WorkerDeps' default `now`. Tests inject a clock, so without this the real
    one ships unexercised — and a naive datetime here would raise on comparison
    against the job's tz-aware `submitted_at`."""
    from llmbus.processing import _utcnow

    stamped = _utcnow()
    assert stamped.tzinfo is timezone.utc
    assert abs((datetime.now(timezone.utc) - stamped).total_seconds()) < 5
