"""Iggy consumer shell for the worker (ARCHITECTURE.md §5, §6, §9).

This is the thin I/O half of the worker. It pulls jobs off the `llm-jobs` topic
through the `llm-workers` consumer group, hands each one to
`processing.process_job` (the pure core), and lets the SDK commit the offset
**after** the job is processed — at-least-once delivery, made safe by the store's
one-shot `finalize` (§6). Everything model/retry/cost/callback lives in
`processing.py`; this module only wires Iggy, the httpx callback sender, config,
and process lifecycle.

Integration-touching, so it is out of the mutation gate (like store/client) and
its live consume loop is covered by integration tests against a dockerized Iggy,
not the fast suite (those lines carry `# pragma: no cover`). The pure seams —
`decode_job`, `ensure_topology` over an injected client, `make_callback_sender`
over an injected httpx client — are unit-tested with fakes.

`httpx` is imported lazily (worker extra only), so importing this module never
requires it; only `run_worker` — which runs on the worker host, where the extra is
installed — constructs the real client.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import random
import signal
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from apache_iggy import (
    AutoCommit,
    AutoCommitAfter,
    IggyClient,
    PollingStrategy,
    ReceiveMessage,
)
from pydantic import ValidationError

from llmbus.config import (
    Config,
    build_providers,
    iggy_connection_string,
    parse_config,
    parse_connect_policy,
    parse_worker_policy,
)
from llmbus.processing import CallbackSender, WorkerDeps, callback_headers, process_job
from llmbus.ratelimit import RateLimiter
from llmbus.retry import RetryPolicy, WorkerPolicy, backoff_delay
from llmbus.schema import Job
from llmbus.store import Store

_log = logging.getLogger("llmbus.worker")

# How much of a poison message's body to echo into the drop log — enough to debug
# the offending producer without dumping an unbounded payload.
_POISON_LOG_BYTES = 500


@dataclasses.dataclass(frozen=True)
class Topology:
    """Where the worker consumes (§5). Defaults are the fixed v1 names — one
    stream, one topic, one partition, one consumer group. Injectable so integration
    tests can use a unique stream/topic per test (the SDK's own isolation pattern),
    not because v1 tunes it."""

    stream: str = "llmbus"
    topic: str = "llm-jobs"
    consumer_group: str = "llm-workers"
    partitions: int = 1


DEFAULT_TOPOLOGY = Topology()


def decode_job(payload: bytes) -> Job:
    """Parse a raw `llm-jobs` message body into a `Job` (§4 contract).

    Raises `pydantic.ValidationError` for malformed JSON *or* a body that breaks
    the contract (`extra="forbid"`, missing/typed fields) — pydantic's
    `model_validate_json` reports both as `ValidationError`. The consume loop turns
    that into a dropped poison message rather than a wedged partition.
    """
    return Job.model_validate_json(payload)


# Builds a *fresh*, connection-string-configured IggyClient (so `connect()` also
# authenticates — see config.iggy_connection_string). Injected (like config.py's SDK
# `ClientFactory`) so `connect_broker` can throw away a poisoned client between
# attempts, and so unit tests drive the handshake with a fake, not a live broker.
IggyClientFactory = Callable[[], IggyClient]


@dataclasses.dataclass(frozen=True)
class BackoffEffects:
    """The two effects a backoff loop needs, injected as one bundle.

    Same pattern and reason as `processing.WorkerDeps`: the real clock and RNG are
    the defaults, tests override them so no real time passes and the jitter is
    deterministic. Bundled rather than passed as two more parameters to keep the
    call signature inside the repo's argument budget (ruff PLR0913).
    """

    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    rand: Callable[[], float] = random.random


async def connect_broker(
    make_client: IggyClientFactory,
    policy: RetryPolicy,
    effects: BackoffEffects = BackoffEffects(),
) -> IggyClient:
    """Connect to the broker, retrying with jittered backoff (§6, §14 #16).

    There is deliberately no `login_user` call here. `make_client` builds a
    connection-string client (`config.iggy_connection_string`), so the SDK owns
    authentication and performs it *inside* `connect()` — on the first connect and on
    every internal reconnect. Logging in by hand is what made the worker crash with
    `Unauthenticated` after the SDK silently reconnected (§14 #16); do not add it back.

    Retries **any** exception rather than consulting `is_retryable`, which is scoped to
    provider errors (§14 #12) and would call a broker `RuntimeError` terminal. It costs
    nothing to be broad here: the unit sets `Restart=always`, so every failure already
    ends in a retry via a process restart — this just reaches the same state without a
    traceback in the journal or the 5s `RestartSec` gap. Exhausting `policy.max_attempts`
    re-raises, so a misconfigured worker (bad password) still fails loudly.

    Each attempt builds a **fresh** client: a failed connect can leave the previous one
    poisoned (the integration suite's `_connect_or_skip` learned this the same way).

    Not addressed here: an attempt is not individually timed out, so a truly unreachable
    broker can block inside the Rust client instead of cycling attempts. That is
    pre-existing and needs its own timeout knob.
    """
    attempt = 0
    # `while True` for the same reason as processing._call_with_retry: both exits
    # are explicit, leaving no unreachable post-loop line. `attempt` strictly
    # increases and the guard bounds it, so this terminates.
    while True:
        client = make_client()
        try:
            await client.connect()
            return client
        except Exception as exc:  # noqa: BLE001 - any connect failure earns a bounded retry
            if attempt + 1 >= policy.max_attempts:
                raise
            _log.warning(
                "broker connect failed (attempt %d/%d): %s — retrying",
                attempt + 1,
                policy.max_attempts,
                exc,
            )
            await effects.sleep(backoff_delay(attempt, policy, effects.rand()))
            attempt += 1


async def ensure_topology(client: IggyClient, topology: Topology = DEFAULT_TOPOLOGY) -> None:
    """Create the stream and topic if they don't exist yet (idempotent, §5).

    Check-then-create is race-free enough for v1's single worker; the consumer
    group is created by `consumer_group(create_consumer_group_if_not_exists=True)`.
    """
    if await client.get_stream(topology.stream) is None:
        await client.create_stream(topology.stream)
    if await client.get_topic(topology.stream, topology.topic) is None:
        await client.create_topic(topology.stream, topology.topic, topology.partitions)


def make_callback_sender(http_client: Any, secret: str | None = None) -> CallbackSender:
    """A `CallbackSender` that POSTs the result over an injected httpx client, signed
    with an HMAC over the raw body when `secret` is set (§14 #19).

    Serializes the payload once and posts *those* bytes (`content=`, not `json=`), so
    the signature in the headers covers exactly what goes on the wire — the receiver
    hashes the raw request body it gets. The client is injected so tests drive it with
    a fake / `httpx.MockTransport` (no network) and `run_worker` owns its lifecycle. A
    non-2xx response raises; `processing._deliver` logs and swallows it, since callbacks
    are best-effort and the store/poll path is the reliable one (§6).
    """

    async def send(url: str, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        headers = callback_headers(secret, body)
        response = await http_client.post(url, content=body, headers=headers)
        response.raise_for_status()

    return send


async def _consume_one(deps: WorkerDeps, message: ReceiveMessage) -> None:
    """Decode one message and process it; drop (log + skip) a poison message.

    A body that won't parse into a `Job` has no valid `job_id` to finalize and can
    never succeed, so it is logged (with a truncated raw payload) and skipped,
    letting the offset commit past it — halting or retrying would wedge the single
    worker on one bad message. A well-formed job goes to `process_job`, which owns
    the store write and callback; its `Result` is already durable, so it is ignored
    here.
    """
    payload = message.payload()
    try:
        job = decode_job(payload)
    except ValidationError as exc:
        _log.warning(
            "dropping poison message at offset %s: %s | raw=%r",
            message.offset(),
            exc,
            payload[:_POISON_LOG_BYTES],
        )
        return
    await process_job(deps, job)


def _load(env: Mapping[str, str] | None) -> tuple[Config, WorkerPolicy, RetryPolicy]:
    """Resolve the environment (real `.env` when `env is None`) and parse the
    shared `Config`, the worker-only `WorkerPolicy`, and the broker-handshake
    `RetryPolicy` (§14 #16) from it."""
    if env is None:  # pragma: no cover - real .env path, only hit from run_worker
        from dotenv import load_dotenv

        load_dotenv()
        env = os.environ
    return parse_config(env), parse_worker_policy(env), parse_connect_policy(env)


async def run_worker(
    env: Mapping[str, str] | None = None,
    *,
    shutdown: asyncio.Event | None = None,
    topology: Topology = DEFAULT_TOPOLOGY,
) -> None:  # pragma: no cover - live Iggy loop, covered by integration tests
    """Run the consumer loop until `shutdown` is set (or forever).

    Wires config → providers/rate-limiter/store/callback into `WorkerDeps`, joins
    the consumer group, and consumes with commit-after-each-message. All resources
    are torn down in `finally` so a shutdown (SIGINT/SIGTERM via `main`) closes the
    store connection and the httpx client cleanly.
    """
    config, policy, connect_policy = _load(env)

    import httpx

    http_client = httpx.AsyncClient(timeout=policy.job_timeout_s)
    store = Store(config.db_path)
    shutdown = shutdown if shutdown is not None else asyncio.Event()
    try:
        await store.connect()
        # Publish before consuming: a producer that connects while this worker is
        # still joining the group should still read a current policy (§14 #21).
        await store.publish_worker_policy(policy)
        client = await connect_broker(
            lambda: IggyClient.from_connection_string(
                iggy_connection_string(
                    config.iggy_address, config.iggy_username, config.iggy_password
                )
            ),
            connect_policy,
        )
        await ensure_topology(client, topology)

        deps = WorkerDeps(
            providers=build_providers(config),
            rate_limiter=RateLimiter(config.rate_limits),
            store=store,
            policy=policy,
            deliver_callback=make_callback_sender(http_client, config.callback_secret),
        )
        consumer = await client.consumer_group(
            topology.consumer_group,
            topology.stream,
            topology.topic,
            polling_strategy=PollingStrategy.Next(),
            auto_commit=AutoCommit.After(AutoCommitAfter.ConsumingEachMessage()),
        )

        async def on_message(message: ReceiveMessage) -> None:
            await _consume_one(deps, message)

        _log.info(
            "worker consuming %s/%s as group %s",
            topology.stream,
            topology.topic,
            topology.consumer_group,
        )
        await consumer.consume_messages(on_message, shutdown)
    finally:
        await store.close()
        await http_client.aclose()


async def _main() -> None:  # pragma: no cover - process entrypoint
    shutdown = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, shutdown.set)
    await run_worker(shutdown=shutdown)


def main() -> None:  # pragma: no cover - process entrypoint
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())


if __name__ == "__main__":  # pragma: no cover
    main()
