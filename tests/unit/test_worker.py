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

from llmbus.processing import WorkerDeps
from llmbus.providers.base import ProviderResult
from llmbus.retry import RetryPolicy, WorkerPolicy
from llmbus.schema import Job, JobParams, Message, Usage
from llmbus.store import Store
from llmbus.worker import (
    DEFAULT_TOPOLOGY,
    Topology,
    _consume_one,
    _load,
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
    }
    config, policy = _load(env)
    assert config.db_path == "worker.db"
    assert config.iggy_address == "127.0.0.1:8090"
    assert policy.retry.max_attempts == 4
    assert policy.job_timeout_s == 60
    assert policy.default_output_tokens == 512
