"""Producer-side bus client (ARCHITECTURE.md §3, §5, §9, §14 #7).

The half of the bus other projects import. `submit()` places a `Job` on the
`llm-jobs` topic and writes its `pending` row to the shared store; `await_result()`
polls that same store by `job_id` until the worker finalizes it. Results do **not**
flow back through Iggy (§4/§5) — the store is the request/reply channel, and the
poll path is the reliable one (the worker's callback is best-effort, §6).

Producer-only by construction: this module imports Iggy, the store, and the message
contract — all **core** deps — but never the LLM SDKs or httpx (the `worker` extra).
Importing `client` keeps a producer lean (§10, §14 #3), which is the whole reason
the SDKs live behind an extra.

Everything is async end-to-end (the store is aiosqlite, the Iggy client asyncio-only);
no sync wrappers in v1.

Testability mirrors `worker.py`: the pure seams — `encode_job`, `send_job` over an
injected Iggy client, `poll_result` over an injected store, `result_from_stored` —
are unit-tested with fakes; `BusClient` wires them and owns the
connection lifecycle, itself injectable so no unit test opens a socket or the real
`.env`. Like `worker.py`/`store.py` it is integration-touching, so it is out of the
mutation gate (CLAUDE.md) but still owes coverage + a live round-trip test.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass

from apache_iggy import IggyClient, SendMessage

from llmbus.config import Config, load_config
from llmbus.schema import Job, Result
from llmbus.store import Store, StoredJob
from llmbus.worker import DEFAULT_TOPOLOGY, Topology, ensure_topology

_log = logging.getLogger("llmbus.client")


@dataclass(frozen=True)
class IggyLogin:
    """The producer's Iggy credentials (§10). Bundled so `BusClient` takes its two
    backends (Iggy, store) plus one login, like the codebase's other frozen config
    dataclasses — and so `connect()` has one place to read them from."""

    username: str
    password: str


# The v1 topic has a single partition; the SDK indexes partitions from 0, so the
# producer always targets partition 0 — the same 0-index the worker consumes (the
# fix behind the worker integration test).
_SEND_PARTITION = 0

# Defaults for await_result's poll loop. Named + per-call overridable rather than
# magic numbers: how long a caller waits for a result and how often it re-reads the
# store are client-side API defaults (like an HTTP client's default timeout), not
# deployment policy, so they are not `.env` settings. Neither is a tuned v1 number.
DEFAULT_RESULT_TIMEOUT_S = 30.0
DEFAULT_POLL_INTERVAL_S = 0.1


def encode_job(job: Job) -> SendMessage:
    """Serialize a `Job` into a wire `SendMessage` (§4 body JSON).

    Uses `model_dump_json()` — the exact form the worker's `decode_job`
    (`Job.model_validate_json`) round-trips. `Job` has no aliased fields, so
    `by_alias` is moot here; the `in`/`out` aliases live only on `Usage`, on the
    Result path. All metadata rides in the body because the SDK has no message
    headers (§4).
    """
    return SendMessage(job.model_dump_json())


async def send_job(client: IggyClient, job: Job, topology: Topology = DEFAULT_TOPOLOGY) -> None:
    """Place one `Job` on the topic's single partition (§5).

    Over an injected client so unit tests drive a fake and never touch a broker;
    `BusClient.submit` passes the real one.
    """
    await client.send_messages(topology.stream, topology.topic, _SEND_PARTITION, [encode_job(job)])


def result_from_stored(stored: StoredJob) -> Result:
    """Project a **terminal** `StoredJob` row back into the `Result` contract (§4).

    The store row carries extras the `Result` model omits (`project`/`model`/
    `submitted_at`/`completed_at`); this keeps exactly the `Result` fields. Only
    called on terminal rows, where `status` is `ok`/`error` — the `Literal` the
    model requires. Goes through `model_validate` so a row that somehow is not a
    valid result (e.g. a stray `pending`) raises `ValidationError` rather than being
    handed back as a malformed `Result`.
    """
    return Result.model_validate(
        {
            "job_id": stored.job_id,
            "status": stored.status,
            "completion": stored.completion,
            "usage": stored.usage,
            "provider": stored.provider,
            "error": stored.error,
            "meta": stored.meta,
        }
    )


async def poll_result(
    store: Store,
    job_id: str,
    *,
    timeout_s: float = DEFAULT_RESULT_TIMEOUT_S,
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
) -> Result:
    """Poll the shared store for `job_id`'s terminal `Result`, or raise `TimeoutError`.

    The reliable delivery path (§6/§14 #7): `submit()` wrote the `pending` row, the
    worker flips it terminal, and this reads it back. Re-reads every `poll_interval_s`
    until the row is terminal or `timeout_s` (monotonic) elapses. The row is checked
    **before** the deadline, so a job that is already done returns even with
    `timeout_s=0`. A never-submitted `job_id` simply reads as absent and times out —
    the same outcome as a job that never completes.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        stored = await store.get(job_id)
        if stored is not None and stored.is_terminal:
            return result_from_stored(stored)
        if time.monotonic() >= deadline:
            raise TimeoutError(f"no terminal result for job {job_id} after {timeout_s}s")
        await asyncio.sleep(poll_interval_s)


class BusClient:
    """Producer handle over a shared store + Iggy connection.

    Other projects construct one (usually via `from_env()`), `connect()` it once,
    then `submit()` jobs and `await_result()` on them. Owns an `IggyClient` (to
    produce) and a `Store` (to poll); both are injected so unit tests drive fakes and
    never open a socket or read the real `.env`. An async context manager, so the
    common path is `async with BusClient.from_env() as bus: ...`.
    """

    def __init__(
        self,
        *,
        iggy: IggyClient,
        store: Store,
        login: IggyLogin,
        topology: Topology = DEFAULT_TOPOLOGY,
    ) -> None:
        self._iggy = iggy
        self._store = store
        self._login = login
        self._topology = topology

    @classmethod
    def from_config(cls, config: Config, *, topology: Topology = DEFAULT_TOPOLOGY) -> BusClient:
        """Build a client from a resolved `Config`: the shared Iggy address/creds and
        the store path (§10). Constructs the SDK client and store offline — neither
        connects until `connect()`."""
        return cls(
            iggy=IggyClient(config.iggy_address),
            store=Store(config.db_path),
            login=IggyLogin(config.iggy_username, config.iggy_password),
            topology=topology,
        )

    @classmethod
    def from_env(
        cls, env: Mapping[str, str] | None = None, *, topology: Topology = DEFAULT_TOPOLOGY
    ) -> BusClient:
        """Build a client from `.env` (or an injected mapping, for tests). Resolves
        only the shared `Config` — a producer never needs the worker-only policy keys
        (§10, §14 #11)."""
        return cls.from_config(load_config(env), topology=topology)

    async def connect(self) -> None:
        """Open the store + Iggy connection, log in, and ensure the topology exists.

        Idempotent topology creation (§5) means a producer that races ahead of the
        worker still finds the topic to send to — it does not depend on the worker
        having booted first.
        """
        await self._store.connect()
        await self._iggy.connect()
        await self._iggy.login_user(self._login.username, self._login.password)
        await ensure_topology(self._iggy, self._topology)

    async def close(self) -> None:
        """Release the store connection. Idempotent (see `Store.close`). Mirrors the
        worker, which likewise does not explicitly close the SDK's Iggy client."""
        await self._store.close()

    async def __aenter__(self) -> BusClient:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def submit(self, job: Job) -> str:
        """Place a `Job` on the bus and return its `job_id` (§3, §14 #7).

        Writes the `pending` row to the shared store **first**, then sends to Iggy —
        so `await_result` (and the lag count, §11) can see the job even in the window
        before a worker consumes it. A duplicate `job_id` is a no-op at the store
        (first write wins, §6) but is still sent; the worker's one-shot `finalize`
        makes redelivery safe. Returns immediately — the model call happens later on a
        worker (§3).
        """
        await self._store.insert_pending(job)
        await send_job(self._iggy, job, self._topology)
        _log.debug(
            "submitted job %s to %s/%s", job.job_id, self._topology.stream, self._topology.topic
        )
        return job.job_id

    async def await_result(
        self,
        job_id: str,
        *,
        timeout_s: float = DEFAULT_RESULT_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
    ) -> Result:
        """Block until `job_id` has a terminal `Result`, or raise `TimeoutError`.

        Polls the shared store (§14 #7) — the reliable path, independent of the
        best-effort callback (§6). For batch/script producers that submit then wait;
        callback producers ignore this and let the worker POST the result (§3).
        """
        return await poll_result(
            self._store, job_id, timeout_s=timeout_s, poll_interval_s=poll_interval_s
        )
