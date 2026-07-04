"""Unit tests for the producer client's pure seams (ARCHITECTURE.md §3, §14 #7).

`client.py` is I/O-touching (like worker/store), so it is out of the mutation gate
but still owes coverage. The live connect path is proven by the integration suite;
here we test everything else against fakes and an in-memory store: `encode_job`,
`send_job` over a fake Iggy client, `result_from_stored`, `poll_result` (already-done
/ waits-then-done / times out, with injected clock+sleep), and `BusClient`'s
`submit`/`await_result`/`connect`/`close`/context-manager + `from_env`/`from_config`.
"""

import asyncio

import pytest
from apache_iggy import SendMessage

from llmbus.client import (
    DEFAULT_POLL_INTERVAL_S,
    DEFAULT_RESULT_TIMEOUT_S,
    BusClient,
    IggyLogin,
    encode_job,
    poll_result,
    result_from_stored,
    send_job,
)
from llmbus.schema import Job, JobParams, Message, Result, Usage
from llmbus.store import Store, StoredJob
from llmbus.worker import DEFAULT_TOPOLOGY, Topology

# --- fakes -------------------------------------------------------------------


class FakeIggyClient:
    """Records the producer/topology calls a `BusClient` makes; duck-types the SDK."""

    def __init__(self, *, stream=None, topic=None):
        self._stream = stream
        self._topic = topic
        self.sent = []
        self.connected = False
        self.logins = []
        self.created_streams = []
        self.created_topics = []

    async def connect(self):
        self.connected = True

    async def login_user(self, username, password):
        self.logins.append((username, password))

    async def send_messages(self, stream, topic, partition, messages):
        self.sent.append((stream, topic, partition, messages))

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


def ok_result(job_id, completion="classified"):
    return Result(
        job_id=job_id,
        status="ok",
        completion=completion,
        provider="openai",
        usage=Usage(input_tokens=10, output_tokens=20, cost_usd=0.003),
    )


def _valid_env():
    return {
        "OPENAI_API_KEY": "sk-o",
        "ANTHROPIC_API_KEY": "sk-a",
        "OPENAI_RPM": "500",
        "OPENAI_TPM": "200000",
        "ANTHROPIC_RPM": "50",
        "ANTHROPIC_TPM": "40000",
        "IGGY_ADDRESS": "127.0.0.1:8090",
        "IGGY_USERNAME": "iggy",
        "IGGY_PASSWORD": "iggy",
        "STORE_PATH": ":memory:",
    }


def make_client(store, iggy=None, **overrides):
    kwargs = {
        "iggy": iggy if iggy is not None else FakeIggyClient(),
        "store": store,
        "login": IggyLogin("iggy", "secret"),
    }
    kwargs.update(overrides)
    return BusClient(**kwargs)


# --- encode_job --------------------------------------------------------------


def test_encode_job_produces_a_sendmessage():
    assert isinstance(encode_job(make_job()), SendMessage)


def test_encode_job_body_round_trips_through_the_worker_decode():
    # The wire body is exactly what the worker's Job.model_validate_json parses back
    # — encode_job and decode_job are mirrors across the bus (§4).
    job = make_job(meta={"comment_id": "7"})
    decoded = Job.model_validate_json(job.model_dump_json())
    assert decoded.job_id == job.job_id
    assert decoded.meta == {"comment_id": "7"}


# --- send_job ----------------------------------------------------------------


async def test_send_job_targets_the_single_partition_zero():
    client = FakeIggyClient()
    job = make_job()
    await send_job(client, job)
    assert len(client.sent) == 1
    stream, topic, partition, messages = client.sent[0]
    assert (stream, topic, partition) == (DEFAULT_TOPOLOGY.stream, DEFAULT_TOPOLOGY.topic, 0)
    assert len(messages) == 1
    assert isinstance(messages[0], SendMessage)


async def test_send_job_uses_injected_topology():
    client = FakeIggyClient()
    await send_job(client, make_job(), Topology(stream="s1", topic="t1"))
    stream, topic, partition, _ = client.sent[0]
    assert (stream, topic, partition) == ("s1", "t1", 0)


# --- result_from_stored ------------------------------------------------------


def _stored(**overrides):
    from datetime import datetime, timezone

    data = {
        "job_id": "11111111-1111-1111-1111-111111111111",
        "project": "hate-moderator",
        "model": "gpt-5-nano",
        "status": "ok",
        "completion": "classified",
        "error": None,
        "provider": "openai",
        "usage": Usage(input_tokens=10, output_tokens=20, cost_usd=0.003),
        "meta": {"comment_id": "7"},
        "submitted_at": datetime(2026, 7, 4, tzinfo=timezone.utc),
        "completed_at": datetime(2026, 7, 4, tzinfo=timezone.utc),
    }
    data.update(overrides)
    return StoredJob(**data)


def test_result_from_stored_maps_an_ok_row():
    result = result_from_stored(_stored())
    assert result.job_id == "11111111-1111-1111-1111-111111111111"
    assert result.status == "ok"
    assert result.completion == "classified"
    assert result.provider == "openai"
    assert result.error is None
    assert result.usage == Usage(input_tokens=10, output_tokens=20, cost_usd=0.003)
    assert result.meta == {"comment_id": "7"}


def test_result_from_stored_maps_an_error_row():
    result = result_from_stored(
        _stored(status="error", completion=None, error="boom", provider=None)
    )
    assert result.status == "error"
    assert result.completion is None
    assert result.error == "boom"
    assert result.provider is None


def test_result_from_stored_rejects_a_non_terminal_row():
    # Defensive: a 'pending' status is not a valid Result (Literal["ok","error"]).
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        result_from_stored(_stored(status="pending"))


# --- poll_result -------------------------------------------------------------


async def test_poll_result_returns_immediately_when_already_terminal():
    async with Store(":memory:") as store:
        job = make_job()
        await store.insert_pending(job)
        await store.finalize(ok_result(job.job_id))

        # timeout_s=0: the row is checked before the deadline, so an already-done job
        # returns without waiting.
        result = await poll_result(store, job.job_id, timeout_s=0.0)

        assert result.status == "ok"
        assert result.completion == "classified"


async def test_poll_result_waits_then_returns_when_the_row_becomes_terminal():
    # The core behaviour: the row is `pending` on the first read and becomes terminal
    # while poll_result loops. A concurrent task finalizes it ~one poll in; a generous
    # timeout keeps the test from ever racing the deadline.
    async with Store(":memory:") as store:
        job = make_job()
        await store.insert_pending(job)

        async def _finalize_soon():
            await asyncio.sleep(0.02)
            await store.finalize(ok_result(job.job_id, completion="late"))

        finalizer = asyncio.create_task(_finalize_soon())
        result = await poll_result(store, job.job_id, timeout_s=5.0, poll_interval_s=0.01)
        await finalizer

        assert result.completion == "late"


async def test_poll_result_times_out_when_never_finalized():
    async with Store(":memory:") as store:
        job = make_job()
        await store.insert_pending(job)  # stays pending forever

        with pytest.raises(TimeoutError, match=job.job_id):
            await poll_result(store, job.job_id, timeout_s=0.05, poll_interval_s=0.01)


async def test_poll_result_times_out_for_an_unknown_job_id():
    async with Store(":memory:") as store:
        with pytest.raises(TimeoutError):
            await poll_result(store, "unknown-id", timeout_s=0.05, poll_interval_s=0.01)


# --- BusClient.submit --------------------------------------------------------


async def test_submit_inserts_pending_then_sends_and_returns_job_id():
    async with Store(":memory:") as store:
        iggy = FakeIggyClient()
        bus = make_client(store, iggy)
        job = make_job()

        returned = await bus.submit(job)

        assert returned == job.job_id
        stored = await store.get(job.job_id)
        assert stored is not None and stored.status == "pending"
        assert len(iggy.sent) == 1
        stream, topic, partition, _ = iggy.sent[0]
        assert (stream, topic, partition) == (DEFAULT_TOPOLOGY.stream, DEFAULT_TOPOLOGY.topic, 0)


async def test_submit_of_a_duplicate_job_id_is_store_noop_but_still_sends():
    async with Store(":memory:") as store:
        iggy = FakeIggyClient()
        bus = make_client(store, iggy)
        job = make_job()

        first = await bus.submit(job)
        second = await bus.submit(job)  # same job_id

        assert first == second == job.job_id
        assert await store.pending_count() == 1  # first write wins (§6)
        assert len(iggy.sent) == 2  # but both were sent; redelivery is worker-safe


async def test_submit_uses_the_injected_topology():
    async with Store(":memory:") as store:
        iggy = FakeIggyClient()
        bus = make_client(store, iggy, topology=Topology(stream="s9", topic="t9"))
        await bus.submit(make_job())
        stream, topic, _, _ = iggy.sent[0]
        assert (stream, topic) == ("s9", "t9")


# --- BusClient.await_result --------------------------------------------------


async def test_await_result_returns_the_terminal_result():
    async with Store(":memory:") as store:
        job = make_job()
        bus = make_client(store)
        await bus.submit(job)
        await store.finalize(ok_result(job.job_id))

        result = await bus.await_result(job.job_id, timeout_s=1.0, poll_interval_s=0.01)

        assert result.job_id == job.job_id
        assert result.status == "ok"


async def test_await_result_times_out_on_a_pending_job():
    async with Store(":memory:") as store:
        job = make_job()
        bus = make_client(store)
        await bus.submit(job)  # never finalized

        with pytest.raises(TimeoutError):
            await bus.await_result(job.job_id, timeout_s=0.05, poll_interval_s=0.01)


# --- BusClient lifecycle -----------------------------------------------------


async def test_connect_opens_store_logs_in_and_ensures_topology():
    iggy = FakeIggyClient(stream=None, topic=None)  # nothing exists yet
    store = Store(":memory:")
    bus = make_client(store, iggy)

    await bus.connect()
    try:
        assert iggy.connected is True
        assert iggy.logins == [("iggy", "secret")]
        assert iggy.created_streams == [DEFAULT_TOPOLOGY.stream]
        assert iggy.created_topics == [
            (DEFAULT_TOPOLOGY.stream, DEFAULT_TOPOLOGY.topic, DEFAULT_TOPOLOGY.partitions)
        ]
        assert await store.pending_count() == 0  # store is open and usable
    finally:
        await bus.close()


async def test_context_manager_connects_on_enter_and_closes_on_exit():
    iggy = FakeIggyClient(stream=object(), topic=object())  # topology already present
    store = Store(":memory:")

    async with make_client(store, iggy) as bus:
        assert iggy.connected is True
        job = make_job()
        await bus.submit(job)
        assert (await store.get(job.job_id)) is not None

    # After exit the store connection is released; using it raises.
    with pytest.raises(RuntimeError, match="not connected"):
        await store.get(job.job_id)


# --- constructors ------------------------------------------------------------


def test_from_config_builds_a_client_without_connecting():
    from llmbus.config import parse_config

    config = parse_config(_valid_env())
    bus = BusClient.from_config(config)
    assert isinstance(bus, BusClient)


def test_from_env_builds_a_client_from_an_injected_mapping():
    bus = BusClient.from_env(_valid_env())
    assert isinstance(bus, BusClient)


def test_from_env_passes_through_a_custom_topology():
    topo = Topology(stream="sX", topic="tX")
    bus = BusClient.from_env(_valid_env(), topology=topo)
    assert bus._topology == topo


def test_await_result_defaults_are_named_constants():
    # The v1 poll defaults are exposed (not magic numbers) so callers can see them.
    assert DEFAULT_RESULT_TIMEOUT_S == 30.0
    assert DEFAULT_POLL_INTERVAL_S == 0.1
