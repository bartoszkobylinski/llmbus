"""Unit tests for the worker's pure seams (ARCHITECTURE.md §5, §6).

The live Iggy consume loop (`run_worker`) needs a server and is covered by the
integration suite; here we test the pieces that don't: `decode_job` (contract +
poison classification), `ensure_topology` over a fake client, `make_callback_sender`
over a fake (duck-typed) client, `_consume_one` (decode → process, poison drop), and
`_load` (config/policy parsing). worker.py is I/O, so it is excluded from the
mutation gate but still owes coverage.
"""

import pytest
from pydantic import ValidationError

from llmbus.config import Config
from llmbus.processing import WorkerDeps
from llmbus.providers.base import ProviderResult
from llmbus.retry import RetryPolicy, WorkerPolicy, is_retryable
from llmbus.schema import Job, JobParams, Message, Usage
from llmbus.store import Store
from llmbus.worker import (
    DEFAULT_TOPOLOGY,
    BackoffEffects,
    Topology,
    _consume_one,
    _load,
    connect_broker,
    decode_job,
    ensure_topology,
    make_callback_sender,
)

# --- fakes -------------------------------------------------------------------


class FakeProvider:
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
    def __init__(self):
        self.acquired = []

    async def acquire(self, provider, tokens):
        self.acquired.append((provider, tokens))


class FakeCallback:
    def __init__(self):
        self.deliveries = []

    async def __call__(self, url, payload):
        self.deliveries.append((url, payload))


class FakeMessage:
    """Stands in for a Rust `ReceiveMessage` (payload bytes + offset)."""

    def __init__(self, payload, offset=0):
        self._payload = payload
        self._offset = offset

    def payload(self):
        return self._payload

    def offset(self):
        return self._offset


class _HandshakeClient:
    """One connect attempt: raises `error` from `connect()`, else succeeds.

    `login_calls` exists purely so a test can assert `connect_broker` NEVER calls
    `login_user` — a real connection-string client authenticates inside `connect()`,
    and a manual login is the §14 #16 bug.
    """

    def __init__(self, error=None):
        self._error = error
        self.connect_calls = 0
        self.login_calls = 0

    async def connect(self):
        self.connect_calls += 1
        if self._error is not None:
            raise self._error

    async def login_user(self, username, password):
        self.login_calls += 1


class _HandshakeFactory:
    """Hands out one pre-scripted client per attempt and keeps every client it built.

    `errors[i]` is the failure for attempt i (None = that attempt succeeds); past
    the end of the list, attempts succeed. `self.clients` is what lets a test assert
    the retry builds a FRESH client each time rather than reusing a poisoned one.
    """

    def __init__(self, errors):
        self._errors = list(errors)
        self.clients = []

    def __call__(self):
        index = len(self.clients)
        error = self._errors[index] if index < len(self._errors) else None
        client = _HandshakeClient(error)
        self.clients.append(client)
        return client


class FakeIggyClient:
    """Records topology calls; get_* return what the test pre-seeds (or None)."""

    def __init__(self, *, stream=None, topic=None):
        self._stream = stream
        self._topic = topic
        self.created_streams = []
        self.created_topics = []

    async def get_stream(self, name):
        return self._stream

    async def get_topic(self, stream, topic):
        return self._topic

    async def create_stream(self, name):
        self.created_streams.append(name)

    async def create_topic(self, stream, name, partitions):
        self.created_topics.append((stream, name, partitions))


# --- builders ----------------------------------------------------------------


def make_job(**overrides):
    data = {
        "project": "hate-moderator",
        "kind": "classify",
        "model": "gpt-5-nano",
        "messages": [Message(role="user", content="hello world")],
        "params": JobParams(max_tokens=8),
        "meta": {},
    }
    data.update(overrides)
    return Job(**data)


def make_config(**overrides):
    data = {
        "openai_api_key": "sk-o",
        "anthropic_api_key": "sk-a",
        "rate_limits": {},
        "iggy_address": "127.0.0.1:8090",
        "iggy_username": "iggy-user",
        "iggy_password": "iggy-pass",
        "db_path": "worker.db",
    }
    data.update(overrides)
    return Config(**data)


def connect_policy(max_attempts=3, base_delay_s=0.25, max_delay_s=5.0):
    return RetryPolicy(
        max_attempts=max_attempts, base_delay_s=base_delay_s, max_delay_s=max_delay_s
    )


class _RecordingSleep:
    """Async sleep that records every delay instead of passing real time."""

    def __init__(self):
        self.delays = []

    async def __call__(self, delay):
        self.delays.append(delay)


def build_deps(store, provider):
    return WorkerDeps(
        providers={provider.name: provider},
        rate_limiter=FakeRateLimiter(),
        store=store,
        policy=WorkerPolicy(
            retry=RetryPolicy(max_attempts=4, base_delay_s=0.5, max_delay_s=30),
            job_timeout_s=60,
            default_output_tokens=512,
        ),
        deliver_callback=FakeCallback(),
    )


def ok_result(completion="done"):
    return ProviderResult(completion=completion, usage=Usage(input_tokens=10, output_tokens=20))


# --- decode_job --------------------------------------------------------------


def test_decode_job_parses_a_valid_body():
    job = make_job(meta={"comment_id": "7"})
    decoded = decode_job(job.model_dump_json().encode())
    assert decoded.job_id == job.job_id
    assert decoded.project == "hate-moderator"
    assert decoded.meta == {"comment_id": "7"}


def test_decode_job_rejects_malformed_json():
    with pytest.raises(ValidationError):
        decode_job(b"this is not json")


def test_decode_job_rejects_unknown_field():
    # extra="forbid" (§4): a stray field is a poison message, not silently dropped.
    body = b'{"project":"p","kind":"classify","model":"gpt-5-nano","messages":[],"typo":1}'
    with pytest.raises(ValidationError):
        decode_job(body)


def test_decode_job_rejects_missing_required_field():
    with pytest.raises(ValidationError):
        decode_job(b'{"project":"p"}')


def test_default_topology_pins_v1_consumer_group_and_single_partition():
    assert DEFAULT_TOPOLOGY == Topology(
        stream="llmbus",
        topic="llm-jobs",
        consumer_group="llm-workers",
        partitions=1,
    )


# --- ensure_topology ---------------------------------------------------------


async def test_ensure_topology_creates_both_when_absent():
    client = FakeIggyClient(stream=None, topic=None)
    await ensure_topology(client, DEFAULT_TOPOLOGY)
    assert client.created_streams == ["llmbus"]
    assert client.created_topics == [("llmbus", "llm-jobs", 1)]


async def test_ensure_topology_creates_nothing_when_present():
    client = FakeIggyClient(stream=object(), topic=object())
    await ensure_topology(client, DEFAULT_TOPOLOGY)
    assert client.created_streams == []
    assert client.created_topics == []


async def test_ensure_topology_creates_only_the_missing_topic():
    client = FakeIggyClient(stream=object(), topic=None)
    await ensure_topology(client, DEFAULT_TOPOLOGY)
    assert client.created_streams == []
    assert client.created_topics == [("llmbus", "llm-jobs", 1)]


async def test_ensure_topology_uses_injected_names():
    client = FakeIggyClient(stream=None, topic=None)
    await ensure_topology(client, Topology(stream="s1", topic="t1", partitions=3))
    assert client.created_streams == ["s1"]
    assert client.created_topics == [("s1", "t1", 3)]


# --- make_callback_sender ----------------------------------------------------
#
# The sender duck-types its client (`post(...).raise_for_status()`), so a plain
# fake stands in for httpx — the logic is covered in the frozen gate (no extra),
# and the real httpx client is exercised by run_worker + the integration suite.


class _FakeResponse:
    def __init__(self, status_code):
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttpClient:
    def __init__(self, status_code=200):
        self.status_code = status_code
        self.posts = []

    async def post(self, url, json):
        self.posts.append((url, json))
        return _FakeResponse(self.status_code)


async def test_callback_sender_posts_json_payload():
    client = FakeHttpClient(status_code=200)
    send = make_callback_sender(client)

    await send("http://cb/internal", {"job_id": "1", "status": "ok"})

    assert client.posts == [("http://cb/internal", {"job_id": "1", "status": "ok"})]


async def test_callback_sender_raises_on_non_2xx():
    send = make_callback_sender(FakeHttpClient(status_code=500))
    with pytest.raises(RuntimeError, match="HTTP 500"):
        await send("http://cb", {"a": 1})


# --- _consume_one ------------------------------------------------------------


async def test_consume_one_processes_a_valid_job():
    async with Store(":memory:") as store:
        job = make_job()
        await store.insert_pending(job)
        provider = FakeProvider("openai", [ok_result(completion="classified")])
        deps = build_deps(store, provider)

        await _consume_one(deps, FakeMessage(job.model_dump_json().encode()))

        assert len(provider.calls) == 1
        stored = await store.get(job.job_id)
        assert stored.status == "ok"
        assert stored.completion == "classified"


async def test_consume_one_drops_a_poison_message(caplog):
    async with Store(":memory:") as store:
        provider = FakeProvider("openai", [])  # must never be called
        deps = build_deps(store, provider)

        await _consume_one(deps, FakeMessage(b'{"not":"a job"}', offset=7))  # must not raise

        assert provider.calls == []
        assert "dropping poison message at offset 7" in caplog.text


async def test_consume_one_truncates_poison_payload_in_log(caplog):
    async with Store(":memory:") as store:
        provider = FakeProvider("openai", [])
        deps = build_deps(store, provider)
        payload = b'{"not":"a job","padding":"' + (b"x" * 1_000) + b'"}'

        await _consume_one(deps, FakeMessage(payload, offset=9))

        assert provider.calls == []
        assert "dropping poison message at offset 9" in caplog.text
        assert "raw=" in caplog.text
        # Truncated to the first 500 bytes of the raw payload (prefix + ~474 x's),
        # not the full 1000-x body — asserted precisely and independent of the
        # JSON prefix length via the logged repr.
        assert repr(payload[:500]) in caplog.text
        assert repr(payload) not in caplog.text


# --- connect_broker ----------------------------------------------------------
#
# Broker connect + retry (§6, §14 #16). Authentication is NOT here: the client comes
# from a connection string, so the SDK logs in inside connect() and re-logs in on its
# own internal reconnects. Logging in by hand left auto_login Disabled, so the SDK's
# reconnect (send_raw_with_response -> disconnect -> connect -> retry) came back
# unauthenticated and the worker died with RuntimeError("Unauthenticated") on prod.


async def test_connect_broker_never_logs_in_by_hand():
    # THE regression guard for §14 #16. A connection-string client authenticates inside
    # connect(); calling login_user here would mean the client was built the wrong way
    # (auto_login Disabled), which is exactly the bug that crashed prod after a
    # reconnect. If someone re-adds the login, this fails.
    factory = _HandshakeFactory([])

    await connect_broker(factory, connect_policy(), BackoffEffects(rand=lambda: 1.0))

    assert [c.login_calls for c in factory.clients] == [0]


async def test_connect_broker_returns_the_client_on_a_clean_connect():
    factory = _HandshakeFactory([])
    sleep = _RecordingSleep()

    client = await connect_broker(
        factory, connect_policy(), BackoffEffects(sleep=sleep, rand=lambda: 1.0)
    )

    assert client is factory.clients[0]
    assert len(factory.clients) == 1
    assert client.connect_calls == 1
    assert sleep.delays == []


async def test_connect_broker_retries_the_iggy_disconnected_runtime_error():
    # `is_retryable` calls this terminal (it is a bare RuntimeError — no status_code,
    # no SDK class name), which is why connect retries ANY exception instead of
    # consulting it. If someone later routes this through `is_retryable`, this fails.
    assert not is_retryable(RuntimeError("Disconnected"))
    factory = _HandshakeFactory([RuntimeError("Disconnected")])
    sleep = _RecordingSleep()

    client = await connect_broker(
        factory, connect_policy(), BackoffEffects(sleep=sleep, rand=lambda: 1.0)
    )

    assert client is factory.clients[1]
    assert len(sleep.delays) == 1


async def test_connect_broker_builds_a_fresh_client_for_every_attempt():
    # A failed handshake can leave the client poisoned, so a retry must not reuse
    # it. Each attempt gets its own client, and only the last one is logged in.
    factory = _HandshakeFactory([RuntimeError("Disconnected"), RuntimeError("Disconnected")])

    client = await connect_broker(
        factory,
        connect_policy(),
        BackoffEffects(sleep=_RecordingSleep(), rand=lambda: 1.0),
    )

    assert len(factory.clients) == 3
    assert client is factory.clients[2]
    assert [c.connect_calls for c in factory.clients] == [1, 1, 1]


async def test_connect_broker_retries_a_plain_connection_error_too():
    # Not just the Iggy RuntimeError: a genuinely cold/absent broker (ConnectionError)
    # is the other real case this retry exists for.
    factory = _HandshakeFactory([ConnectionError("refused")])

    client = await connect_broker(
        factory,
        connect_policy(),
        BackoffEffects(sleep=_RecordingSleep(), rand=lambda: 1.0),
    )

    assert client is factory.clients[1]


async def test_connect_broker_reraises_the_last_error_once_attempts_are_spent():
    # Exhaustion must still exit: a genuinely misconfigured worker (bad password)
    # fails loudly and lets systemd take over, exactly as before this change.
    last = RuntimeError("still down")
    factory = _HandshakeFactory([RuntimeError("down"), RuntimeError("down"), last])
    sleep = _RecordingSleep()

    with pytest.raises(RuntimeError, match="still down"):
        await connect_broker(
            factory,
            connect_policy(max_attempts=3),
            BackoffEffects(sleep=sleep, rand=lambda: 1.0),
        )

    assert len(factory.clients) == 3  # exactly max_attempts, no more
    assert len(sleep.delays) == 2  # no sleep after the final failure


async def test_connect_broker_backs_off_exponentially_with_injected_jitter():
    factory = _HandshakeFactory([RuntimeError("x"), RuntimeError("x"), RuntimeError("x")])
    sleep = _RecordingSleep()

    await connect_broker(
        factory,
        connect_policy(max_attempts=5, base_delay_s=0.25, max_delay_s=5.0),
        BackoffEffects(sleep=sleep, rand=lambda: 1.0),  # rand=1.0 -> uncapped ceiling
    )

    assert sleep.delays == [0.25, 0.5, 1.0]


async def test_connect_broker_caps_backoff_at_max_delay():
    factory = _HandshakeFactory([RuntimeError("x")] * 4)
    sleep = _RecordingSleep()

    await connect_broker(
        factory,
        connect_policy(max_attempts=6, base_delay_s=1.0, max_delay_s=2.0),
        BackoffEffects(sleep=sleep, rand=lambda: 1.0),
    )

    assert sleep.delays == [1.0, 2.0, 2.0, 2.0]  # 4.0 and 8.0 clamped to the cap


async def test_connect_broker_applies_the_injected_jitter_to_backoff():
    factory = _HandshakeFactory([RuntimeError("x"), RuntimeError("x")])
    sleep = _RecordingSleep()

    await connect_broker(
        factory,
        connect_policy(max_attempts=4, base_delay_s=2.0, max_delay_s=10.0),
        BackoffEffects(sleep=sleep, rand=lambda: 0.25),
    )

    assert sleep.delays == [0.5, 1.0]


async def test_connect_broker_single_attempt_policy_does_not_retry():
    factory = _HandshakeFactory([RuntimeError("nope")])
    sleep = _RecordingSleep()

    with pytest.raises(RuntimeError, match="nope"):
        await connect_broker(
            factory,
            connect_policy(max_attempts=1),
            BackoffEffects(sleep=sleep, rand=lambda: 1.0),
        )

    assert len(factory.clients) == 1
    assert sleep.delays == []


# --- _load -------------------------------------------------------------------


def test_load_parses_config_and_worker_policy():
    env = {
        "OPENAI_API_KEY": "sk-o",
        "ANTHROPIC_API_KEY": "sk-a",
        "OPENAI_RPM": "500",
        "OPENAI_TPM": "200000",
        "ANTHROPIC_RPM": "50",
        "ANTHROPIC_TPM": "40000",
        "IGGY_ADDRESS": "127.0.0.1:8090",
        "IGGY_USERNAME": "iggy",
        "IGGY_PASSWORD": "iggy",
        "STORE_PATH": "worker.db",
        "WORKER_MAX_ATTEMPTS": "4",
        "WORKER_BACKOFF_BASE_S": "0.5",
        "WORKER_BACKOFF_MAX_S": "30",
        "WORKER_JOB_TIMEOUT_S": "60",
        "WORKER_DEFAULT_OUTPUT_TOKENS": "512",
        "WORKER_CONNECT_MAX_ATTEMPTS": "10",
        "WORKER_CONNECT_BACKOFF_BASE_S": "0.25",
        "WORKER_CONNECT_BACKOFF_MAX_S": "5",
    }
    config, policy, connect = _load(env)
    assert config.db_path == "worker.db"
    assert config.iggy_address == "127.0.0.1:8090"
    assert policy.retry.max_attempts == 4
    assert policy.job_timeout_s == 60
    assert policy.default_output_tokens == 512
    # The handshake policy is parsed separately and stays independent of the job
    # retry above (§14 #16) — retuning one must not silently retune the other.
    assert connect.max_attempts == 10
    assert connect.base_delay_s == 0.25
    assert connect.max_delay_s == 5
