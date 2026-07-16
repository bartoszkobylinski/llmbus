"""Live producer round-trip (marker: `integration`) — needs a dockerized Iggy.

Proves the actual product flow the unit suite can't: a `BusClient.submit()` places a
real message on `llm-jobs` and writes a `pending` store row; the worker consume side
finalizes it; `BusClient.await_result()` then reads the terminal `Result` back by
polling the shared store (§3, §14 #7). Providers and the callback are faked, so no
real model call happens; Iggy and the SQLite store are real.

Isolation + skip-when-down follow the worker integration test: a unique
stream/topic/group per run, and a bounded connect+login retry against a possibly
cold broker.

    docker compose up -d
    uv run pytest -m integration
"""

import asyncio
import os
from uuid import uuid4

import pytest

pytest.importorskip("httpx", reason="integration wiring needs the `worker` extra")

from apache_iggy import (  # noqa: E402
    AutoCommit,
    AutoCommitAfter,
    IggyClient,
    PollingStrategy,
)

from llmbus.client import BusClient  # noqa: E402
from llmbus.config import iggy_connection_string  # noqa: E402
from llmbus.processing import WorkerDeps  # noqa: E402
from llmbus.providers.base import ProviderResult  # noqa: E402
from llmbus.retry import RetryPolicy, WorkerPolicy  # noqa: E402
from llmbus.schema import Job, JobParams, Message, Usage  # noqa: E402
from llmbus.store import Store  # noqa: E402
from llmbus.worker import Topology, _consume_one, ensure_topology  # noqa: E402

pytestmark = pytest.mark.integration

_ADDR = os.environ.get("IGGY_ADDRESS", "127.0.0.1:8090")
_USER = os.environ.get("IGGY_USERNAME", "iggy")
_PASS = os.environ.get("IGGY_PASSWORD", "iggy")


async def _connect_or_skip() -> IggyClient:
    # A freshly-started broker binds its TCP port before it is protocol-ready, so a
    # cold connect gets 'Disconnected'; retry the bounded connect with a fresh client
    # each attempt. (Same helper the worker integration test uses.)
    last_exc: BaseException | None = None
    for _ in range(20):
        # from_connection_string, not IggyClient(addr)+login_user: only this form sets
        # the SDK's auto_login, so connect() authenticates and the SDK re-authenticates
        # on its own internal reconnects (§14 #16). Mirrors production exactly.
        client = IggyClient.from_connection_string(iggy_connection_string(_ADDR, _USER, _PASS))
        try:
            await asyncio.wait_for(client.connect(), timeout=3)
            return client
        except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001 - retry any handshake failure
            last_exc = exc
            await asyncio.sleep(1)
    if os.environ.get("LLMBUS_REQUIRE_IGGY"):
        raise RuntimeError(f"Iggy at {_ADDR} not usable after retries: {last_exc}")
    pytest.skip(f"local Iggy not reachable at {_ADDR}: {last_exc}")


def _unique_topology() -> Topology:
    suffix = uuid4().hex[:12]
    return Topology(stream=f"s-{suffix}", topic=f"t-{suffix}", consumer_group=f"g-{suffix}")


class _FakeProvider:
    name = "openai"

    def __init__(self):
        self.calls = 0

    async def call(self, model, messages, params):
        self.calls += 1
        return ProviderResult(completion="classified", usage=Usage(input_tokens=3, output_tokens=1))


class _FakeRateLimiter:
    async def acquire(self, provider, tokens):
        return None


class _FakeCallback:
    async def __call__(self, url, payload):
        return None


def _job() -> Job:
    return Job(
        project="hate-moderator",
        kind="classify",
        model="gpt-5-nano",
        messages=[Message(role="user", content="hello")],
        params=JobParams(max_tokens=8),
    )


def _deps(store, provider):
    return WorkerDeps(
        providers={provider.name: provider},
        rate_limiter=_FakeRateLimiter(),
        store=store,
        policy=WorkerPolicy(
            retry=RetryPolicy(max_attempts=4, base_delay_s=0.5, max_delay_s=30),
            job_timeout_s=60,
            default_output_tokens=512,
        ),
        deliver_callback=_FakeCallback(),
    )


async def _drain_one(client: IggyClient, topology: Topology, deps: WorkerDeps) -> None:
    """Consume exactly one message off the topic and process it (the worker half)."""
    consumer = await client.consumer_group(
        topology.consumer_group,
        topology.stream,
        topology.topic,
        polling_strategy=PollingStrategy.Next(),
        auto_commit=AutoCommit.After(AutoCommitAfter.ConsumingEachMessage()),
    )
    shutdown = asyncio.Event()

    async def on_message(message):
        await _consume_one(deps, message)
        shutdown.set()  # stop after the first message

    await asyncio.wait_for(consumer.consume_messages(on_message, shutdown), timeout=20)


async def test_submit_consume_await_round_trip(tmp_path):
    client = await _connect_or_skip()
    topology = _unique_topology()

    async with Store(str(tmp_path / "store.db")) as store:
        await ensure_topology(client, topology)
        bus = BusClient(
            iggy=client,
            store=store,
            topology=topology,
        )

        job = _job()
        job_id = await bus.submit(job)

        # The pending row is visible immediately, before any worker runs (§11).
        pre = await store.get(job_id)
        assert pre is not None and pre.status == "pending"

        # Run the worker consume side against the same real topic, then poll.
        provider = _FakeProvider()
        await _drain_one(client, topology, _deps(store, provider))

        result = await bus.await_result(job_id, timeout_s=10, poll_interval_s=0.05)

        assert provider.calls == 1
        assert result.job_id == job_id
        assert result.status == "ok"
        assert result.completion == "classified"
        assert result.usage.output_tokens == 1
