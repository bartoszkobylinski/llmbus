"""Live end-to-end worker test (marker: `integration`) — needs a dockerized Iggy.

Verifies the one thing the unit suite can't: that a job sent to a real `llm-jobs`
topic is consumed through a real consumer group, processed by `_consume_one` /
`process_job`, and finalized in the store — with the offset committed **after**
processing (commit-after semantics, §6). Providers and the callback are faked, so
no real model call or HTTP request happens; only Iggy is real.

Isolation: a unique stream/topic/consumer-group per test (uuid suffix), the same
pattern the SDK's own tests use, so runs never collide on the shared dev broker.
Skips cleanly when the broker is unreachable (`docker compose up -d` not run).

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
    SendMessage,
)

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
    # Bound the connect: the Rust client retries a down broker for a long time, so
    # without a timeout an absent Iggy would hang the suite instead of skipping.
    client = IggyClient(_ADDR)
    try:
        await asyncio.wait_for(client.connect(), timeout=5)
        await asyncio.wait_for(client.login_user(_USER, _PASS), timeout=5)
    except (Exception, asyncio.TimeoutError) as exc:  # noqa: BLE001 - any failure ⇒ no broker, skip
        pytest.skip(f"local Iggy not reachable at {_ADDR}: {exc}")
    return client


def _unique_topology() -> Topology:
    suffix = uuid4().hex[:12]
    return Topology(stream=f"s-{suffix}", topic=f"t-{suffix}", consumer_group=f"g-{suffix}")


class _FakeProvider:
    name = "openai"

    def __init__(self):
        self.calls = 0

    async def call(self, model, messages, params):
        self.calls += 1
        return ProviderResult(completion="classified", usage=Usage(input_tokens=1, output_tokens=1))


class _FakeRateLimiter:
    async def acquire(self, provider, tokens):
        return None


class _FakeCallback:
    def __init__(self):
        self.deliveries = []

    async def __call__(self, url, payload):
        self.deliveries.append((url, payload))


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


async def test_worker_consumes_and_finalizes_a_real_message(tmp_path):
    client = await _connect_or_skip()
    topology = _unique_topology()
    await ensure_topology(client, topology)

    async with Store(str(tmp_path / "store.db")) as store:
        job = _job()
        await store.insert_pending(job)  # the submit() side inserts the pending row
        await client.send_messages(
            topology.stream, topology.topic, 1, [SendMessage(job.model_dump_json())]
        )

        provider = _FakeProvider()
        deps = _deps(store, provider)
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

        assert provider.calls == 1
        stored = await store.get(job.job_id)
        assert stored is not None
        assert stored.status == "ok"
        assert stored.completion == "classified"
