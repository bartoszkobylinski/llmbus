"""Unit tests for the SQLite results store (§5, §11).

Exercises the real aiosqlite store against a temp DB file (no Iggy, no network),
so it runs in the fast suite. store.py is I/O, so it is excluded from the mutmut
gate but still owes ≥90% coverage.
"""

from datetime import datetime, timezone

import pytest

from llmbus.schema import Job, Message, Result, Usage
from llmbus.store import PENDING, Store, StoredJob

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


async def test_methods_require_connect(tmp_path):
    store = Store(_db(tmp_path))
    with pytest.raises(RuntimeError, match="not connected"):
        await store.get(_ABSENT_ID)


async def test_close_is_safe_without_connect_and_twice(tmp_path):
    store = Store(_db(tmp_path))
    await store.close()  # never connected
    await store.connect()
    await store.close()
    await store.close()  # double close


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


async def test_meta_round_trips_nested_structures(tmp_path):
    meta = {"comment_id": "9", "nested": {"a": 1}, "labels": ["x", "y"], "flag": False}
    async with Store(_db(tmp_path)) as store:
        job = _job(meta=meta)
        await store.insert_pending(job)
        stored = await store.get(job.job_id)
        assert stored.meta == meta


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
