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

from llmbus.retry import WorkerPolicy, retry_budget_seconds
from llmbus.schema import Job, Result, Usage

# The store-only initial state; terminal statuses ("ok"/"error") mirror `Result`.
PENDING = "pending"

_JOBS_SCHEMA = """
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

# The worker's effective run policy, republished on every worker boot (§14 #21).
# Operator visibility into what the running worker is configured with, plus a
# necessary-but-not-sufficient signal for a polling producer's wait.
# `retry_budget_s` EXCLUDES the rate-limiter wait (no static ceiling), so it is
# not a guarantee — cost safety is `Job.ttl_s` (§14 #22).
# Single row: `id` is pinned to 1, so a boot overwrites rather than accumulates.
_WORKER_POLICY_SCHEMA = """
CREATE TABLE IF NOT EXISTS worker_policy (
    id             INTEGER PRIMARY KEY CHECK (id = 1),
    max_attempts   INTEGER NOT NULL,
    job_timeout_s  REAL    NOT NULL,
    base_delay_s   REAL    NOT NULL,
    max_delay_s    REAL    NOT NULL,
    retry_budget_s REAL    NOT NULL,
    updated_at     TEXT    NOT NULL
);
"""

# Central model policy (§14 #23): which model a `(project, kind)` runs on, so the
# choice lives in ONE place instead of in every producer's config. Keyed by the
# pair rather than by project alone so two tasks in one project can differ
# (`training.analyze` vs `series_classify`). `kind` stays a domain label the
# worker never interprets (§14 #1) — it is a lookup key here, not dispatch.
_MODEL_POLICY_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_policy (
    project    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    model      TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (project, kind)
);
"""

_SCHEMA = _JOBS_SCHEMA + _WORKER_POLICY_SCHEMA + _MODEL_POLICY_SCHEMA

# Columns `worker_policy` is expected to have. `CREATE TABLE IF NOT EXISTS` is a
# no-op against an existing table with a DIFFERENT shape, so a renamed column
# would leave the old schema in place and every publish would fail with
# "table worker_policy has no column named …" — taking the worker down before it
# consumes anything. Checked and repaired on connect instead.
_WORKER_POLICY_COLUMNS = frozenset(
    {
        "id",
        "max_attempts",
        "job_timeout_s",
        "base_delay_s",
        "max_delay_s",
        "retry_budget_s",
        "updated_at",
    }
)


async def _rebuild_worker_policy_if_stale(db: aiosqlite.Connection) -> None:
    """Drop and recreate `worker_policy` if its shape is stale.

    Called only from `publish_worker_policy`, i.e. only by the worker, and only
    inside that method's `BEGIN IMMEDIATE` transaction. Both parts matter:

    - **Writer-only.** Every process calls `connect()`, so doing this there let
      any producer drop a table the worker had just published to. Producers only
      ever read this table, and `read_worker_policy` tolerates a stale shape by
      reporting "nothing published" — so they need no repair path at all.
    - **Inside one write transaction.** `PRAGMA table_info` followed by an
      unguarded `DROP` is check-then-act across connections: concurrent openers
      could each see the old shape and race to drop and recreate, which in
      practice surfaced as "database is locked". `BEGIN IMMEDIATE` takes the
      write lock up front, so the inspection and the repair are one step and
      other writers wait on `busy_timeout` instead of colliding.

    Dropping is safe for this table and no other: it holds derived state the
    worker republishes on every boot. `jobs` is real data and is never touched.
    """
    cursor = await db.execute("PRAGMA table_info(worker_policy)")
    rows = await cursor.fetchall()
    if rows and {row[1] for row in rows} != _WORKER_POLICY_COLUMNS:
        await db.execute("DROP TABLE worker_policy")
        await db.execute(_WORKER_POLICY_SCHEMA)


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


@dataclass(frozen=True)
class ProjectDayCost:
    """Total USD spend for one `project` on one `day` (the cost ledger, §6/§11).

    `day` is the `YYYY-MM-DD` of the job's `submitted_at` (the same date `cost.py`
    prices at, §6), so a day's spend is grouped exactly as it was billed.
    """

    project: str
    day: str
    cost_usd: float


@dataclass(frozen=True)
class ModelPolicy:
    """Which model a `(project, kind)` currently runs on (§14 #23).

    `updated_at` is carried so an operator can see when the choice last changed —
    the same reason `PublishedWorkerPolicy` carries one.
    """

    project: str
    kind: str
    model: str
    updated_at: datetime


@dataclass(frozen=True)
class PublishedWorkerPolicy:
    """What the running worker published about its own run policy (§14 #21).

    `retry_budget_s` covers attempts and backoff only — it EXCLUDES the
    rate-limiter wait before each attempt, which has no static ceiling (see
    `retry.retry_budget_seconds`). It is therefore a necessary-but-not-sufficient
    signal for a producer's wait, not a guarantee; cost safety comes from
    `Job.ttl_s` (§14 #22). The inputs are carried alongside it so an operator can
    see why it is that value, and `updated_at` shows which boot published it, so
    a value from a since-reconfigured worker is detectable rather than merely
    wrong.
    """

    max_attempts: int
    job_timeout_s: float
    base_delay_s: float
    max_delay_s: float
    retry_budget_s: float
    updated_at: datetime


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

    async def cost_by_project_day(self) -> list[ProjectDayCost]:
        """The cost ledger: total USD per project per day (§6/§11).

        Groups on the `YYYY-MM-DD` prefix of the stored ISO `submitted_at` — a
        literal substring, so it never reinterprets the timestamp's offset — and
        sums `cost_usd`. `HAVING SUM(...) > 0` omits groups with no actual spend
        (pending-only, error-only, or genuinely-free completions): a spend ledger
        lists real spend, not `$0.00` rows for days a project merely had activity.
        Ordered by day then project for a stable read.
        """
        cursor = await self._conn.execute(
            """
            SELECT project, substr(submitted_at, 1, 10) AS day, SUM(cost_usd) AS total
              FROM jobs
             GROUP BY project, day
            HAVING SUM(cost_usd) > 0
             ORDER BY day, project
            """
        )
        rows = await cursor.fetchall()
        return [ProjectDayCost(row["project"], row["day"], float(row["total"])) for row in rows]

    async def publish_worker_policy(
        self, policy: WorkerPolicy, updated_at: datetime | None = None
    ) -> None:
        """Record the running worker's policy so producers can read it (§14 #21).

        Called once per worker boot. Upserts the single pinned row, so the store
        always describes the worker that is running now, not the one that ran
        first. `updated_at` is injectable to keep the write testable without
        freezing the clock.
        """
        stamped = updated_at if updated_at is not None else datetime.now(timezone.utc)
        # BEGIN IMMEDIATE takes the write lock before the shape inspection, so
        # repair-then-write is one atomic step. Without it, check-then-act races
        # another opener and shows up as "database is locked".
        await self._conn.execute("BEGIN IMMEDIATE")
        await _rebuild_worker_policy_if_stale(self._conn)
        await self._conn.execute(
            """
            INSERT INTO worker_policy
                (id, max_attempts, job_timeout_s, base_delay_s, max_delay_s, retry_budget_s,
                 updated_at)
            VALUES (1, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                max_attempts  = excluded.max_attempts,
                job_timeout_s = excluded.job_timeout_s,
                base_delay_s  = excluded.base_delay_s,
                max_delay_s   = excluded.max_delay_s,
                retry_budget_s  = excluded.retry_budget_s,
                updated_at    = excluded.updated_at
            """,
            (
                policy.retry.max_attempts,
                policy.job_timeout_s,
                policy.retry.base_delay_s,
                policy.retry.max_delay_s,
                retry_budget_seconds(policy),
                stamped.isoformat(),
            ),
        )
        await self._conn.commit()

    async def read_worker_policy(self) -> PublishedWorkerPolicy | None:
        """The policy the worker published, or None if no worker has ever booted
        against this store.

        None is a real, expected state — a producer may legitimately submit
        before the worker's first start (§5: topology creation is idempotent and
        does not depend on worker order), so the caller decides whether an
        unknown policy is a warning or a refusal.
        """
        cursor = await self._conn.execute("SELECT * FROM worker_policy WHERE id = 1")
        row = await cursor.fetchone()
        if row is None:
            return None
        if set(row.keys()) != _WORKER_POLICY_COLUMNS:
            # A stale-shaped table left by an older worker. Readers do not repair
            # it — only the worker does, on its next boot — so report it as
            # "nothing published", which callers already handle.
            return None
        return PublishedWorkerPolicy(
            max_attempts=int(row["max_attempts"]),
            job_timeout_s=float(row["job_timeout_s"]),
            base_delay_s=float(row["base_delay_s"]),
            max_delay_s=float(row["max_delay_s"]),
            retry_budget_s=float(row["retry_budget_s"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    async def set_model_policy(
        self, project: str, kind: str, model: str, updated_at: datetime | None = None
    ) -> None:
        """Point `(project, kind)` at `model`, replacing any previous choice.

        Upsert, so the table describes the choice in force *now* rather than the
        first one ever made — the same reason `publish_worker_policy` upserts.
        `updated_at` is injectable so tests assert an exact value instead of
        racing the clock; production passes nothing and gets now.
        """
        stamp = updated_at or datetime.now(timezone.utc)
        await self._conn.execute(
            """
            INSERT INTO model_policy (project, kind, model, updated_at)
                 VALUES (?, ?, ?, ?)
            ON CONFLICT(project, kind) DO UPDATE SET
                 model = excluded.model, updated_at = excluded.updated_at
            """,
            (project, kind, model, stamp.isoformat()),
        )
        await self._conn.commit()

    async def model_policy(self, project: str, kind: str) -> ModelPolicy | None:
        """The model chosen for `(project, kind)`, or `None` when unset.

        `None` is not an error here — it is the caller's decision what absence
        means. `resolve_model` (schema.py) turns it into a hard failure at submit
        (§14 #23); a UI listing policies just shows nothing for that pair.
        """
        cursor = await self._conn.execute(
            "SELECT project, kind, model, updated_at FROM model_policy "
            "WHERE project = ? AND kind = ?",
            (project, kind),
        )
        row = await cursor.fetchone()
        return None if row is None else _row_to_model_policy(row)

    async def list_model_policies(self) -> list[ModelPolicy]:
        """Every policy row, ordered by project then kind (for the policy page)."""
        cursor = await self._conn.execute(
            "SELECT project, kind, model, updated_at FROM model_policy ORDER BY project, kind"
        )
        return [_row_to_model_policy(row) for row in await cursor.fetchall()]


def _row_to_model_policy(row: aiosqlite.Row) -> ModelPolicy:
    return ModelPolicy(
        project=row["project"],
        kind=row["kind"],
        model=row["model"],
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


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
