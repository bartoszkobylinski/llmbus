"""Unit tests for the message contract (ARCHITECTURE.md §4)."""

import uuid
from datetime import timezone

import pytest
from pydantic import ValidationError

from llmbus.schema import Job, JobParams, Message, Result, Usage


def _minimal_job(**overrides):
    data = {
        "project": "hate-moderator",
        "kind": "classify",
        "model": "gpt-4o-mini",
        "messages": [Message(role="user", content="hi")],
    }
    data.update(overrides)
    return Job(**data)


# --- Job ---------------------------------------------------------------------


def test_job_generates_uuid_job_id():
    uuid.UUID(_minimal_job().job_id)  # raises if not a valid uuid


def test_job_ids_are_unique():
    assert _minimal_job().job_id != _minimal_job().job_id


def test_job_submitted_at_is_utc_aware():
    assert _minimal_job().submitted_at.tzinfo is timezone.utc


def test_job_defaults():
    job = _minimal_job()
    assert job.params == JobParams()
    assert job.callback_url is None
    assert job.meta == {}


def test_job_params_concrete_defaults():
    # Assert concrete values, not just equality to JobParams(), so a mutated
    # default can't hide behind both sides mutating together.
    params = JobParams()
    assert params.temperature == 0.0
    assert params.max_tokens is None
    assert params.response_format is None


def test_job_requires_core_fields():
    with pytest.raises(ValidationError):
        Job(kind="classify", model="x", messages=[])  # missing `project`


def test_job_round_trips_through_dict():
    job = _minimal_job(meta={"comment_id": "42"})
    assert Job.model_validate(job.model_dump()) == job


def test_job_meta_is_preserved_untouched():
    job = _minimal_job(meta={"comment_id": "42", "nested": {"a": 1}})
    assert job.meta == {"comment_id": "42", "nested": {"a": 1}}


# --- Message -----------------------------------------------------------------


def test_message_rejects_unknown_role():
    with pytest.raises(ValidationError):
        Message(role="robot", content="x")


# --- Usage (wire aliases in/out) ---------------------------------------------


def test_usage_serializes_with_wire_aliases():
    dumped = Usage(input_tokens=10, output_tokens=20, cost_usd=0.001).model_dump(by_alias=True)
    assert dumped == {"in": 10, "out": 20, "cost_usd": 0.001}


def test_usage_parses_from_wire_aliases():
    usage = Usage.model_validate({"in": 5, "out": 7, "cost_usd": 0.02})
    assert usage.input_tokens == 5
    assert usage.output_tokens == 7


def test_usage_defaults_to_zero():
    usage = Usage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cost_usd == 0.0


# --- Result ------------------------------------------------------------------


def test_result_status_must_be_ok_or_error():
    with pytest.raises(ValidationError):
        Result(job_id="j", status="pending")


def test_result_ok_defaults():
    result = Result(job_id="j", status="ok", completion="done")
    assert result.usage == Usage()
    assert result.error is None
    assert result.meta == {}


def test_result_error_carries_message_and_meta():
    result = Result(job_id="j", status="error", error="boom", meta={"comment_id": "9"})
    assert result.completion is None
    assert result.error == "boom"
    assert result.meta == {"comment_id": "9"}
