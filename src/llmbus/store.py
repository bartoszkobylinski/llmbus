"""SQLite results store (ARCHITECTURE.md §5, §11).

The store is the bus's request/reply channel: results do **not** flow back through
Iggy (§5) — they land here keyed by `job_id`. `submit()` inserts a `pending` row,
the worker flips it to a terminal `ok`/`error` `Result` after the model call, and
`await_result()` polls this same file until the row is terminal.

Two facts shape the design:

- **Lag without `get_stats`.** The Iggy SDK exposes no `get_stats` (§12), so "how
  many jobs are waiting" is approximated as the count of `pending` rows (§11) —
  which only works because `submit()` writes that pending row up front.
- **Cross-process, single host.** The worker (writer) and a co-located producer
  (poll reader) open the same file (§3, §9b), so the connection runs in **WAL**
  mode with a `busy_timeout`: readers never block the single writer, and a
  transient lock waits on a background thread instead of on the event loop.

SQLite is not sync-wrapped onto the loop: `aiosqlite` runs every call on a
per-connection background thread, so the loop stays free while disk I/O is in
flight (CLAUDE.md: async end-to-end, no sync wrappers). The store persists more
than the `Result` contract carries — `project`, `model`, `submitted_at` — because
cost-per-project/day and dated pricing (§6, §11) need them and `Result` omits them.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import aiosqlite

from llmbus.schema import Job, Result, Usage

# The store-only initial state; terminal statuses ("ok"/"error") mirror `Result`.
PENDING = "pending"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,
    project       TEXT NOT NULL,
    model         TEXT NOT NULL,
    status        TEXT NOT NULL,
    completion    TEXT,
    error         TEXT,
    provider      TEXT,
    input_tokens  INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd      REAL    NOT NULL DEFAULT 0.0,
    meta          TEXT    NOT NULL DEFAULT '{}',
    submitted_at  TEXT    NOT NULL,
    completed_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);
"""


@dataclass(frozen=True)
class StoredJob:
    """One row: a job's lifecycle state in the store.

    `status` is `pending` until the worker finalizes it, then `ok`/`error`
    mirroring the `Result` contract. Carries the Job-derived `project`/`model`/
    `submitted_at` the `Result` model omits (needed for cost accounting, §6/§11).
    """

    job_id: str
    project: str
    model: str
    status: str
    completion: str | None
    error: str | None
    provider: str | None
    usage: Usage
    meta: dict[str, object]
    submitted_at: datetime
    completed_at: datetime | None

    @property
    def is_terminal(self) -> bool:
        """True once the worker has written a final result (not `pending`)."""
        return self.status != PENDING


class Store:
    """Async SQLite results store. One instance owns one aiosqlite connection.

    Each process (the worker, or a polling producer) opens its own `Store` over
    the same file; WAL mode makes that safe for one writer + many readers.
    """

    def __init__(self, db_path: str) -> None:
        self._path = db_path
        self._db: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        """Open the connection, enable WAL + busy_timeout, and create the schema.

        Idempotent DDL (`IF NOT EXISTS`), so reconnecting to an existing store is
        safe. Creates the parent directory so a fresh deploy needn't pre-make it.
        """
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        db = await aiosqlite.connect(self._path)
        db.row_factory = aiosqlite.Row
        # WAL: pollers (readers) never block the single worker (writer). busy_timeout:
        # a transient lock waits on the connection's thread, not the event loop.
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA busy_timeout=5000")
        await db.executescript(_SCHEMA)
        await db.commit()
        self._db = db

    async def close(self) -> None:
        """Close the connection. Safe to call when never connected or twice."""
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> Store:
        await self.connect()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    @property
    def _conn(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store is not connected; call connect() first")
        return self._db

    async def insert_pending(self, job: Job) -> bool:
        """Insert a `pending` row for a freshly-submitted job.

        Returns True if inserted, False if a row for this `job_id` already exists
        — a duplicate submit is a no-op, not an error, and the first write wins.
        """
        cursor = await self._conn.execute(
            """
            INSERT INTO jobs (job_id, project, model, status, meta, submitted_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO NOTHING
            """,
            (
                job.job_id,
                job.project,
                job.model,
                PENDING,
                json.dumps(job.meta),
                job.submitted_at.isoformat(),
            ),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def finalize(self, result: Result, completed_at: datetime | None = None) -> bool:
        """Transition a `pending` row to its terminal `Result`.

        Guarded on `status = 'pending'`, so it is a one-shot transition: the first
        delivery wins and returns True; a redelivered job (at-least-once, §6) finds
        no pending row and returns False — the worker uses that to skip a duplicate
        callback. Returns False too if the job_id was never inserted. `meta`,
        `project`, `model` and `submitted_at` are fixed at insert and left untouched.
        """
        when = completed_at or datetime.now(timezone.utc)
        cursor = await self._conn.execute(
            """
            UPDATE jobs
               SET status = ?, completion = ?, error = ?, provider = ?,
                   input_tokens = ?, output_tokens = ?, cost_usd = ?,
                   completed_at = ?
             WHERE job_id = ? AND status = ?
            """,
            (
                result.status,
                result.completion,
                result.error,
                result.provider,
                result.usage.input_tokens,
                result.usage.output_tokens,
                result.usage.cost_usd,
                when.isoformat(),
                result.job_id,
                PENDING,
            ),
        )
        await self._conn.commit()
        return cursor.rowcount == 1

    async def get(self, job_id: str) -> StoredJob | None:
        """Read a job's current row, or None if the job_id is unknown."""
        cursor = await self._conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,))
        row = await cursor.fetchone()
        if row is None:
            return None
        return _row_to_stored_job(row)

    async def pending_count(self) -> int:
        """Number of jobs still awaiting a result — the lag approximation (§11)."""
        cursor = await self._conn.execute("SELECT COUNT(*) FROM jobs WHERE status = ?", (PENDING,))
        row = await cursor.fetchone()
        assert row is not None  # COUNT(*) always returns exactly one row
        return int(row[0])


def _row_to_stored_job(row: aiosqlite.Row) -> StoredJob:
    """Map a DB row back into a StoredJob (parsing JSON meta + ISO timestamps)."""
    completed_raw = row["completed_at"]
    return StoredJob(
        job_id=row["job_id"],
        project=row["project"],
        model=row["model"],
        status=row["status"],
        completion=row["completion"],
        error=row["error"],
        provider=row["provider"],
        usage=Usage(
            input_tokens=row["input_tokens"],
            output_tokens=row["output_tokens"],
            cost_usd=row["cost_usd"],
        ),
        meta=json.loads(row["meta"]),
        submitted_at=datetime.fromisoformat(row["submitted_at"]),
        completed_at=datetime.fromisoformat(completed_raw) if completed_raw else None,
    )
