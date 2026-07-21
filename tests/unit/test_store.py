"""Unit tests for the SQLite results store (§5, §11).

Exercises the real aiosqlite store against a temp DB file (no Iggy, no network),
so it runs in the fast suite. store.py is I/O, so it is excluded from the mutmut
gate but still owes ≥90% coverage.
"""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from llmbus.retry import RetryPolicy, WorkerPolicy
from llmbus.schema import Job, Message, Result, Usage
from llmbus.store import (
    PENDING,
    ProjectDayCost,
    PublishedWorkerPolicy,
    Store,
    StoredJob,
)

# A fixed submitted_at so round-trips through the store assert an exact value.
_SUBMITTED = datetime(2026, 7, 3, 12, 34, 56, tzinfo=timezone.utc)
# An arbitrary valid UUID for "unknown job_id" cases (never inserted).
_ABSENT_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"


def _job(**overrides):
    data = {
        "project": "hate-moderator",
        "kind": "classify",
        "model": "gpt-5-mini",
        "messages": [Message(role="user", content="hi")],
        "submitted_at": _SUBMITTED,
    }
    data.update(overrides)
    return Job(**data)


def _db(tmp_path):
    return str(tmp_path / "store.db")


# --- connect / lifecycle -----------------------------------------------------


async def test_connect_enables_wal(tmp_path):
    async with Store(_db(tmp_path)) as store:
        cursor = await store._conn.execute("PRAGMA journal_mode")
        row = await cursor.fetchone()
        assert row[0] == "wal"


async def test_connect_sets_busy_timeout(tmp_path):
    async with Store(_db(tmp_path)) as store:
        cursor = await store._conn.execute("PRAGMA busy_timeout")
        row = await cursor.fetchone()
        assert row[0] == 5000


async def test_connect_creates_status_index(tmp_path):
    async with Store(_db(tmp_path)) as store:
        cursor = await store._conn.execute(
            """
            SELECT name
              FROM sqlite_master
             WHERE type = 'index' AND tbl_name = 'jobs'
            """
        )
        rows = await cursor.fetchall()
        assert {row["name"] for row in rows} >= {"sqlite_autoindex_jobs_1", "idx_jobs_status"}


async def test_reconnecting_to_existing_store_is_safe(tmp_path):
    path = _db(tmp_path)
    async with Store(path) as first:
        await first.insert_pending(_job())
    # Re-opening the same file must not fail on CREATE TABLE and keeps the row.
    async with Store(path) as second:
        assert await second.pending_count() == 1


async def test_in_memory_store_skips_directory_creation():
    # Covers the ":memory:" branch that bypasses mkdir; an in-memory DB still works.
    async with Store(":memory:") as store:
        job = _job()
        assert await store.insert_pending(job) is True
        assert await store.get(job.job_id) is not None


async def test_in_memory_store_instances_are_isolated():
    async with Store(":memory:") as first, Store(":memory:") as second:
        job = _job()
        await first.insert_pending(job)

        assert await first.get(job.job_id) is not None
        assert await second.get(job.job_id) is None


async def test_methods_require_connect(tmp_path):
    store = Store(_db(tmp_path))
    with pytest.raises(RuntimeError, match="not connected"):
        await store.get(_ABSENT_ID)
    with pytest.raises(RuntimeError, match="not connected"):
        await store.insert_pending(_job())
    with pytest.raises(RuntimeError, match="not connected"):
        await store.finalize(Result(job_id=_ABSENT_ID, status="ok"))
    with pytest.raises(RuntimeError, match="not connected"):
        await store.pending_count()


async def test_close_is_safe_without_connect_and_twice(tmp_path):
    store = Store(_db(tmp_path))
    await store.close()  # never connected
    await store.connect()
    await store.close()
    await store.close()  # double close


async def test_store_can_be_reused_after_close_and_reconnect(tmp_path):
    store = Store(_db(tmp_path))
    job = _job()

    await store.connect()
    await store.insert_pending(job)
    await store.close()

    with pytest.raises(RuntimeError, match="not connected"):
        await store.get(job.job_id)

    await store.connect()
    try:
        assert await store.get(job.job_id) is not None
    finally:
        await store.close()


# --- insert_pending ----------------------------------------------------------


async def test_insert_pending_then_get_returns_pending_row(tmp_path):
    async with Store(_db(tmp_path)) as store:
        job = _job(meta={"comment_id": "42"})
        assert await store.insert_pending(job) is True

        stored = await store.get(job.job_id)
        assert isinstance(stored, StoredJob)
        assert stored.job_id == job.job_id
        assert stored.status == PENDING
        assert stored.is_terminal is False
        assert stored.project == "hate-moderator"
        assert stored.model == "gpt-5-mini"
        assert stored.completion is None
        assert stored.error is None
        assert stored.provider is None
        assert stored.usage == Usage()
        assert stored.meta == {"comment_id": "42"}
        assert stored.submitted_at == _SUBMITTED
        assert stored.completed_at is None


async def test_insert_pending_duplicate_is_noop(tmp_path):
    async with Store(_db(tmp_path)) as store:
        job = _job()
        assert await store.insert_pending(job) is True
        assert await store.insert_pending(job) is False
        assert await store.pending_count() == 1


async def test_insert_pending_duplicate_does_not_overwrite_original_row(tmp_path):
    async with Store(_db(tmp_path)) as store:
        job_id = "550e8400-e29b-41d4-a716-446655440000"
        original = _job(
            job_id=job_id,
            project="first-project",
            model="gpt-5-mini",
            meta={"comment_id": "first"},
        )
        duplicate = _job(
            job_id=job_id,
            project="second-project",
            model="claude-haiku-4-5",
            meta={"comment_id": "second"},
        )

        assert await store.insert_pending(original) is True
        assert await store.insert_pending(duplicate) is False

        stored = await store.get(job_id)
        assert stored.project == "first-project"
        assert stored.model == "gpt-5-mini"
        assert stored.meta == {"comment_id": "first"}


async def test_meta_round_trips_nested_structures(tmp_path):
    meta = {"comment_id": "9", "nested": {"a": 1}, "labels": ["x", "y"], "flag": False}
    async with Store(_db(tmp_path)) as store:
        job = _job(meta=meta)
        await store.insert_pending(job)
        stored = await store.get(job.job_id)
        assert stored.meta == meta


async def test_unicode_large_completion_and_column_like_meta_keys_round_trip(tmp_path):
    meta = {
        "job_id": "meta-not-column",
        "status": "meta-status",
        "emoji": "zażółć 🚀",
        "nested": {"completion": "not row completion"},
    }
    completion = "done ✅\n" + ("x" * 100_000)

    async with Store(_db(tmp_path)) as store:
        job = _job(meta=meta)
        await store.insert_pending(job)
        result = Result(
            job_id=job.job_id,
            status="ok",
            completion=completion,
            usage=Usage(
                input_tokens=2**40,
                output_tokens=2**40 + 1,
                cost_usd=123456.789012,
            ),
            provider="anthropic",
        )
        await store.finalize(result)

        stored = await store.get(job.job_id)
        assert stored.meta == meta
        assert stored.completion == completion
        assert stored.usage == Usage(
            input_tokens=2**40,
            output_tokens=2**40 + 1,
            cost_usd=123456.789012,
        )


async def test_submitted_at_offset_and_microseconds_round_trip(tmp_path):
    submitted_at = datetime(
        2026,
        7,
        3,
        18,
        4,
        56,
        123456,
        tzinfo=timezone(timedelta(hours=5, minutes=30)),
    )
    async with Store(_db(tmp_path)) as store:
        job = _job(submitted_at=submitted_at)
        await store.insert_pending(job)

        stored = await store.get(job.job_id)
        assert stored.submitted_at == submitted_at


# --- finalize ----------------------------------------------------------------


async def test_finalize_ok_writes_terminal_result(tmp_path):
    async with Store(_db(tmp_path)) as store:
        job = _job()
        await store.insert_pending(job)
        completed = datetime(2026, 7, 3, 12, 35, 0, tzinfo=timezone.utc)
        result = Result(
            job_id=job.job_id,
            status="ok",
            completion="done",
            usage=Usage(input_tokens=10, output_tokens=20, cost_usd=0.003),
            provider="openai",
        )
        assert await store.finalize(result, completed_at=completed) is True

        stored = await store.get(job.job_id)
        assert stored.status == "ok"
        assert stored.is_terminal is True
        assert stored.completion == "done"
        assert stored.error is None
        assert stored.provider == "openai"
        assert stored.usage == Usage(input_tokens=10, output_tokens=20, cost_usd=0.003)
        assert stored.completed_at == completed
        # Fields fixed at insert are untouched by finalize.
        assert stored.project == "hate-moderator"
        assert stored.submitted_at == _SUBMITTED


async def test_finalize_completed_at_with_microseconds_round_trips(tmp_path):
    async with Store(_db(tmp_path)) as store:
        job = _job()
        await store.insert_pending(job)
        completed = datetime(2026, 7, 3, 12, 35, 0, 654321, tzinfo=timezone.utc)

        assert await store.finalize(
            Result(job_id=job.job_id, status="ok"),
            completed_at=completed,
        )

        stored = await store.get(job.job_id)
        assert stored.completed_at == completed


async def test_finalize_error_writes_error_and_defaults_completed_at(tmp_path):
    async with Store(_db(tmp_path)) as store:
        job = _job()
        await store.insert_pending(job)
        result = Result(job_id=job.job_id, status="error", error="boom", provider="anthropic")
        assert await store.finalize(result) is True

        stored = await store.get(job.job_id)
        assert stored.status == "error"
        assert stored.error == "boom"
        assert stored.completion is None
        assert stored.completed_at is not None  # defaulted to now(UTC)
        assert stored.completed_at.tzinfo is timezone.utc


async def test_finalize_is_one_shot_against_redelivery(tmp_path):
    async with Store(_db(tmp_path)) as store:
        job = _job()
        await store.insert_pending(job)
        first = Result(job_id=job.job_id, status="ok", completion="first")
        second = Result(job_id=job.job_id, status="ok", completion="second")

        assert await store.finalize(first) is True
        assert await store.finalize(second) is False  # already terminal

        stored = await store.get(job.job_id)
        assert stored.completion == "first"  # redelivery did not overwrite


async def test_finalize_does_not_overwrite_insert_metadata(tmp_path):
    async with Store(_db(tmp_path)) as store:
        job = _job(meta={"comment_id": "original", "labels": ["needs-review"]})
        await store.insert_pending(job)
        result = Result(
            job_id=job.job_id,
            status="ok",
            completion="done",
            meta={"comment_id": "result-should-not-win"},
        )

        assert await store.finalize(result) is True

        stored = await store.get(job.job_id)
        assert stored.meta == {"comment_id": "original", "labels": ["needs-review"]}


async def test_finalize_unknown_job_returns_false(tmp_path):
    async with Store(_db(tmp_path)) as store:
        assert await store.finalize(Result(job_id=_ABSENT_ID, status="ok")) is False


# --- pending_count (lag) -----------------------------------------------------


async def test_pending_count_tracks_lag(tmp_path):
    async with Store(_db(tmp_path)) as store:
        assert await store.pending_count() == 0
        a, b = _job(), _job()
        await store.insert_pending(a)
        await store.insert_pending(b)
        assert await store.pending_count() == 2

        await store.finalize(Result(job_id=a.job_id, status="ok"))
        assert await store.pending_count() == 1


# --- get ---------------------------------------------------------------------


async def test_get_unknown_job_returns_none(tmp_path):
    async with Store(_db(tmp_path)) as store:
        assert await store.get(_ABSENT_ID) is None


# --- cross-process access (separate connections, same file) ------------------


async def test_second_connection_sees_committed_rows(tmp_path):
    path = _db(tmp_path)
    async with Store(path) as writer, Store(path) as reader:
        job = _job()
        await writer.insert_pending(job)
        stored = await reader.get(job.job_id)
        assert stored is not None
        assert stored.status == PENDING


async def test_second_connection_sees_finalized_row(tmp_path):
    path = _db(tmp_path)
    async with Store(path) as writer, Store(path) as reader:
        job = _job()
        await writer.insert_pending(job)

        assert await reader.pending_count() == 1

        await writer.finalize(Result(job_id=job.job_id, status="ok", completion="done"))
        stored = await reader.get(job.job_id)

        assert stored.status == "ok"
        assert stored.completion == "done"
        assert await reader.pending_count() == 0


async def test_concurrent_finalize_attempts_on_separate_connections_are_one_shot(tmp_path):
    path = _db(tmp_path)
    async with Store(path) as submitter:
        job = _job()
        await submitter.insert_pending(job)

    async with Store(path) as first, Store(path) as second:
        first_result = Result(job_id=job.job_id, status="ok", completion="first")
        second_result = Result(job_id=job.job_id, status="ok", completion="second")

        outcomes = await asyncio.gather(
            first.finalize(first_result),
            second.finalize(second_result),
        )

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == 1

    async with Store(path) as reader:
        stored = await reader.get(job.job_id)
        assert stored.completion in {"first", "second"}
        assert await reader.pending_count() == 0


async def test_many_concurrent_finalize_attempts_on_separate_connections_are_one_shot(tmp_path):
    path = _db(tmp_path)
    async with Store(path) as submitter:
        job = _job()
        await submitter.insert_pending(job)

    stores = [Store(path) for _ in range(8)]
    for store in stores:
        await store.connect()
    try:
        outcomes = await asyncio.gather(
            *(
                store.finalize(Result(job_id=job.job_id, status="ok", completion=f"winner-{index}"))
                for index, store in enumerate(stores)
            )
        )
    finally:
        await asyncio.gather(*(store.close() for store in stores))

    assert outcomes.count(True) == 1
    assert outcomes.count(False) == len(stores) - 1

    async with Store(path) as reader:
        stored = await reader.get(job.job_id)
        assert stored.completion in {f"winner-{index}" for index in range(len(stores))}
        assert await reader.pending_count() == 0


async def test_reader_polling_observes_pending_then_terminal_across_connections(tmp_path):
    path = _db(tmp_path)
    async with Store(path) as writer, Store(path) as reader:
        job = _job()
        await writer.insert_pending(job)

        assert await reader.get(job.job_id) == await writer.get(job.job_id)
        assert await reader.pending_count() == 1

        await writer.finalize(Result(job_id=job.job_id, status="ok", completion="done"))

        stored = await reader.get(job.job_id)
        assert stored.status == "ok"
        assert stored.completion == "done"
        assert await reader.pending_count() == 0


# --- cost_by_project_day (ledger, §6/§11) ------------------------------------


async def _finalized(store, *, project, submitted_at, cost):
    """Insert then finalize an `ok` job so its row carries a terminal cost_usd."""
    job = _job(project=project, submitted_at=submitted_at)
    await store.insert_pending(job)
    await store.finalize(Result(job_id=job.job_id, status="ok", usage=Usage(cost_usd=cost)))
    return job


async def test_cost_by_project_day_empty_store_is_empty(tmp_path):
    async with Store(_db(tmp_path)) as store:
        assert await store.cost_by_project_day() == []


async def test_cost_by_project_day_sums_within_a_project_day(tmp_path):
    day = datetime(2026, 7, 3, 8, 0, tzinfo=timezone.utc)
    async with Store(_db(tmp_path)) as store:
        await _finalized(store, project="a", submitted_at=day, cost=0.5)
        await _finalized(store, project="a", submitted_at=day.replace(hour=20), cost=0.25)
        assert await store.cost_by_project_day() == [ProjectDayCost("a", "2026-07-03", 0.75)]


async def test_cost_by_project_day_separates_projects_and_days_ordered(tmp_path):
    d1 = datetime(2026, 7, 3, tzinfo=timezone.utc)
    d2 = datetime(2026, 7, 4, tzinfo=timezone.utc)
    async with Store(_db(tmp_path)) as store:
        await _finalized(store, project="b", submitted_at=d1, cost=1.0)
        await _finalized(store, project="a", submitted_at=d1, cost=2.0)
        await _finalized(store, project="a", submitted_at=d2, cost=4.0)
        # Ordered by day, then project.
        assert await store.cost_by_project_day() == [
            ProjectDayCost("a", "2026-07-03", 2.0),
            ProjectDayCost("b", "2026-07-03", 1.0),
            ProjectDayCost("a", "2026-07-04", 4.0),
        ]


async def test_cost_by_project_day_pending_and_error_rows_contribute_zero(tmp_path):
    day = datetime(2026, 7, 3, tzinfo=timezone.utc)
    async with Store(_db(tmp_path)) as store:
        await store.insert_pending(_job(project="a", submitted_at=day))  # pending → 0
        errored = _job(project="a", submitted_at=day)
        await store.insert_pending(errored)
        await store.finalize(Result(job_id=errored.job_id, status="error", error="boom"))  # 0 cost
        await _finalized(store, project="a", submitted_at=day, cost=0.5)
        assert await store.cost_by_project_day() == [ProjectDayCost("a", "2026-07-03", 0.5)]


async def test_cost_by_project_day_omits_projects_with_only_zero_cost_rows(tmp_path):
    day = datetime(2026, 7, 3, tzinfo=timezone.utc)
    async with Store(_db(tmp_path)) as store:
        await store.insert_pending(_job(project="pending-only", submitted_at=day))
        errored = _job(project="error-only", submitted_at=day)
        await store.insert_pending(errored)
        await store.finalize(Result(job_id=errored.job_id, status="error", error="boom"))

        assert await store.cost_by_project_day() == []


async def test_cost_by_project_day_groups_the_date_across_times(tmp_path):
    # Same calendar date, different times/microseconds → one grouped row.
    async with Store(_db(tmp_path)) as store:
        await _finalized(
            store,
            project="a",
            submitted_at=datetime(2026, 7, 3, 0, 0, 0, 1, tzinfo=timezone.utc),
            cost=1.0,
        )
        await _finalized(
            store,
            project="a",
            submitted_at=datetime(2026, 7, 3, 23, 59, 59, tzinfo=timezone.utc),
            cost=2.0,
        )
        assert await store.cost_by_project_day() == [ProjectDayCost("a", "2026-07-03", 3.0)]


async def test_cost_by_project_day_uses_literal_submitted_at_date_with_offset(tmp_path):
    async with Store(_db(tmp_path)) as store:
        await _finalized(
            store,
            project="a",
            submitted_at=datetime(
                2026,
                7,
                3,
                23,
                30,
                tzinfo=timezone(timedelta(hours=-3)),
            ),
            cost=1.0,
        )

        assert await store.cost_by_project_day() == [ProjectDayCost("a", "2026-07-03", 1.0)]


# --- worker policy publication (§14 #21) -------------------------------------


def _policy(max_attempts=2, job_timeout_s=30.0, base_delay_s=0.5, max_delay_s=30.0):
    return WorkerPolicy(
        retry=RetryPolicy(
            max_attempts=max_attempts, base_delay_s=base_delay_s, max_delay_s=max_delay_s
        ),
        job_timeout_s=job_timeout_s,
        default_output_tokens=512,
    )


async def test_worker_policy_is_none_before_any_worker_boots(tmp_path):
    # A producer can legitimately connect first (§5: topology creation does not
    # depend on worker order), so "no policy yet" is a state, not an error.
    async with Store(_db(tmp_path)) as store:
        assert await store.read_worker_policy() is None


async def test_publish_then_read_round_trips_every_field(tmp_path):
    async with Store(_db(tmp_path)) as store:
        await store.publish_worker_policy(_policy(), updated_at=_SUBMITTED)
        assert await store.read_worker_policy() == PublishedWorkerPolicy(
            max_attempts=2,
            job_timeout_s=30.0,
            base_delay_s=0.5,
            max_delay_s=30.0,
            retry_budget_s=60.5,
            updated_at=_SUBMITTED,
        )


async def test_published_worst_case_is_derived_not_supplied(tmp_path):
    # The store computes the bound from the policy, so a producer cannot be told
    # a worst case that disagrees with the retry numbers beside it.
    async with Store(_db(tmp_path)) as store:
        await store.publish_worker_policy(_policy(max_attempts=4, job_timeout_s=60.0))
        published = await store.read_worker_policy()
        assert published is not None
        assert published.retry_budget_s == 243.5


async def test_republishing_overwrites_rather_than_accumulating(tmp_path):
    # Every worker boot republishes. The store must describe the worker running
    # NOW — a stale row beside a fresh one would let a producer read either.
    async with Store(_db(tmp_path)) as store:
        await store.publish_worker_policy(_policy(max_attempts=4, job_timeout_s=60.0))
        await store.publish_worker_policy(_policy(max_attempts=2, job_timeout_s=30.0))
        published = await store.read_worker_policy()
        assert published is not None
        assert (published.max_attempts, published.retry_budget_s) == (2, 60.5)
        cursor = await store._conn.execute("SELECT COUNT(*) FROM worker_policy")
        assert (await cursor.fetchone())[0] == 1


async def test_republish_is_visible_as_one_complete_new_row_to_an_existing_reader(
    tmp_path,
):
    path = _db(tmp_path)
    first_boot = datetime(2026, 7, 21, 8, 0, tzinfo=timezone.utc)
    second_boot = datetime(2026, 7, 21, 9, 0, tzinfo=timezone.utc)
    async with Store(path) as worker, Store(path) as producer:
        await worker.publish_worker_policy(
            _policy(max_attempts=4, job_timeout_s=60.0),
            updated_at=first_boot,
        )
        before = await producer.read_worker_policy()

        await worker.publish_worker_policy(
            _policy(max_attempts=2, job_timeout_s=30.0),
            updated_at=second_boot,
        )
        after = await producer.read_worker_policy()

    assert before == PublishedWorkerPolicy(
        max_attempts=4,
        job_timeout_s=60.0,
        base_delay_s=0.5,
        max_delay_s=30.0,
        retry_budget_s=243.5,
        updated_at=first_boot,
    )
    assert after == PublishedWorkerPolicy(
        max_attempts=2,
        job_timeout_s=30.0,
        base_delay_s=0.5,
        max_delay_s=30.0,
        retry_budget_s=60.5,
        updated_at=second_boot,
    )


async def test_publish_stamps_the_current_time_by_default(tmp_path):
    before = datetime.now(timezone.utc)
    async with Store(_db(tmp_path)) as store:
        await store.publish_worker_policy(_policy())
        published = await store.read_worker_policy()
    assert published is not None
    assert before <= published.updated_at <= datetime.now(timezone.utc)
