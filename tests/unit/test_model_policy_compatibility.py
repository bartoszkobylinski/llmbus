"""Compatibility and concurrency boundaries for central model policy."""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from pydantic import BaseModel, ConfigDict, Field

from llmbus import client as client_module
from llmbus.client import BusClient
from llmbus.schema import Job, JobId, JobParams, Message
from llmbus.store import Store
from llmbus.worker import decode_job

_JOB_ID = "11111111-1111-1111-1111-111111111111"
_SUBMITTED_AT = datetime(2026, 7, 23, 10, 0, tzinfo=timezone.utc)


class _LegacyJob(BaseModel):
    """The Job field contract immediately before model became optional."""

    model_config = ConfigDict(extra="forbid")

    job_id: JobId
    project: str
    kind: str
    model: str
    messages: list[Message]
    params: JobParams = Field(default_factory=JobParams)
    callback_url: str | None = None
    meta: dict[str, object] = Field(default_factory=dict)
    submitted_at: datetime
    ttl_s: float | None = None


class _PayloadIggy:
    def __init__(self) -> None:
        self.payloads: list[str] = []

    async def send_messages(
        self,
        stream: str,
        topic: str,
        partition: int,
        messages: list[str],
    ) -> None:
        del stream, topic, partition
        self.payloads.extend(messages)


def _job(**overrides: object) -> Job:
    data: dict[str, object] = {
        "job_id": _JOB_ID,
        "project": "hate-moderator",
        "kind": "classify",
        "model": "gpt-5.4-mini",
        "messages": [Message(role="user", content="classify this")],
        "params": JobParams(max_tokens=8),
        "meta": {"comment_id": "7"},
        "submitted_at": _SUBMITTED_AT,
        "ttl_s": 550.0,
    }
    data.update(overrides)
    return Job(**data)


def _capture_payloads(monkeypatch: pytest.MonkeyPatch) -> _PayloadIggy:
    monkeypatch.setattr(client_module, "SendMessage", lambda payload: payload)
    return _PayloadIggy()


class TestPersistentTopicCompatibility:
    def test_new_schema_decodes_a_frozen_old_schema_message(self) -> None:
        old_payload = (
            b'{"job_id":"11111111-1111-1111-1111-111111111111",'
            b'"project":"hate-moderator","kind":"classify","model":"gpt-5.4-mini",'
            b'"messages":[{"role":"user","content":"classify this"}],'
            b'"params":{"temperature":null,"max_tokens":8,"response_format":null},'
            b'"callback_url":null,"meta":{"comment_id":"7"},'
            b'"submitted_at":"2026-07-23T10:00:00Z","ttl_s":550.0}'
        )

        decoded = decode_job(old_payload)

        assert decoded == _job()
        assert decoded.model == "gpt-5.4-mini"

    async def test_explicit_model_submit_keeps_the_exact_legacy_wire_shape(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        iggy = _capture_payloads(monkeypatch)
        async with Store(":memory:") as store:
            await BusClient(iggy=iggy, store=store).submit(_job())

        assert iggy.payloads == [
            '{"job_id":"11111111-1111-1111-1111-111111111111",'
            '"project":"hate-moderator","kind":"classify","model":"gpt-5.4-mini",'
            '"messages":[{"role":"user","content":"classify this"}],'
            '"params":{"temperature":null,"max_tokens":8,"response_format":null},'
            '"callback_url":null,"meta":{"comment_id":"7"},'
            '"submitted_at":"2026-07-23T10:00:00Z","ttl_s":550.0}'
        ]
        assert _LegacyJob.model_validate_json(iggy.payloads[0]).model == "gpt-5.4-mini"

    async def test_policy_resolved_message_is_accepted_by_the_legacy_decoder(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        iggy = _capture_payloads(monkeypatch)
        async with Store(":memory:") as store:
            await store.set_model_policy(
                "milamber",
                "series_classify",
                "gpt-5.4",
                _SUBMITTED_AT,
            )
            job = _job(
                project="milamber",
                kind="series_classify",
                model=None,
            )

            await BusClient(iggy=iggy, store=store).submit(job)

        legacy = _LegacyJob.model_validate_json(iggy.payloads[0])
        assert legacy.model == "gpt-5.4"
        assert json.loads(iggy.payloads[0])["model"] == "gpt-5.4"


class TestPolicyStoreBoundaries:
    async def test_store_physically_rejects_a_model_less_job(self) -> None:
        job = _job(model=None)
        async with Store(":memory:") as store:
            with pytest.raises(sqlite3.IntegrityError):
                await store.insert_pending(job)

            assert await store.get(job.job_id) is None

    async def test_concurrent_policy_writes_leave_each_submit_internally_consistent(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = str(tmp_path / "policy-race.db")
        iggy = _capture_payloads(monkeypatch)
        producer = Store(path)
        writer = Store(path)
        await producer.connect()
        await writer.connect()
        try:
            await writer.set_model_policy("milamber", "series_classify", "gpt-5.2")
            bus = BusClient(iggy=iggy, store=producer)
            jobs = [
                _job(
                    job_id=f"00000000-0000-0000-0000-{number:012d}",
                    project="milamber",
                    kind="series_classify",
                    model=None,
                )
                for number in range(1, 21)
            ]

            async def change_policy() -> None:
                for model in ("gpt-5.4", "gpt-5.2", "gpt-5.4", "gpt-5.2"):
                    await writer.set_model_policy("milamber", "series_classify", model)
                    await asyncio.sleep(0)

            await asyncio.gather(change_policy(), *(bus.submit(job) for job in jobs))

            wire_models = {
                payload["job_id"]: payload["model"] for payload in map(json.loads, iggy.payloads)
            }
            assert set(wire_models.values()) <= {"gpt-5.2", "gpt-5.4"}
            assert None not in wire_models.values()
            for job in jobs:
                stored = await producer.get(job.job_id)
                assert stored is not None
                assert stored.model == wire_models[job.job_id]
        finally:
            await writer.close()
            await producer.close()

    async def test_duplicate_submit_cannot_drift_from_its_first_stored_model(
        self,
        tmp_path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        path = str(tmp_path / "duplicate-policy.db")
        iggy = _capture_payloads(monkeypatch)
        async with Store(path) as store:
            bus = BusClient(iggy=iggy, store=store)
            job = _job(project="milamber", kind="series_classify", model=None)
            await store.set_model_policy("milamber", "series_classify", "gpt-5.2")
            await bus.submit(job)
            await store.set_model_policy("milamber", "series_classify", "gpt-5.4")

            await bus.submit(job)

            stored = await store.get(job.job_id)

        assert stored is not None
        assert stored.model == "gpt-5.2"
        assert [json.loads(payload)["model"] for payload in iggy.payloads] == [
            stored.model,
            stored.model,
        ]
