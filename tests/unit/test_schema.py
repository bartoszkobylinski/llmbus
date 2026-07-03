"""Unit tests for the message contract (ARCHITECTURE.md §4)."""

import json
import uuid
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from llmbus.schema import Job, JobParams, Message, Result, Usage

# Valid UUIDs for Result tests — job_id must parse as a UUID under the contract.
_JOB_ID = "3f2504e0-4f89-41d3-9a0c-0305e82c3301"
_JOB_ID_2 = "550e8400-e29b-41d4-a716-446655440000"


def _minimal_job(**overrides):
    data = {
        "project": "hate-moderator",
        "kind": "classify",
        "model": "gpt-5-mini",
        "messages": [Message(role="user", content="hi")],
    }
    data.update(overrides)
    return Job(**data)


# --- Job ---------------------------------------------------------------------


def test_job_generates_uuid_job_id():
    job_id = _minimal_job().job_id
    uuid.UUID(job_id)  # raises if not a valid uuid
    assert str(uuid.UUID(job_id)) == job_id


def test_job_ids_are_unique():
    assert _minimal_job().job_id != _minimal_job().job_id


def test_job_submitted_at_is_utc_aware():
    assert _minimal_job().submitted_at.tzinfo is timezone.utc


def test_job_defaults():
    job = _minimal_job()
    assert job.params == JobParams()
    assert job.callback_url is None
    assert job.meta == {}


def test_job_default_containers_are_not_shared():
    first = _minimal_job()
    second = _minimal_job()

    first.params.temperature = 0.7
    first.meta["comment_id"] = "42"
    first.messages.append(Message(role="assistant", content="done"))

    assert second.params == JobParams()
    assert second.meta == {}
    assert second.messages == [Message(role="user", content="hi")]


def test_job_params_concrete_defaults():
    # Assert concrete values, not just equality to JobParams(), so a mutated
    # default can't hide behind both sides mutating together.
    params = JobParams()
    assert params.temperature is None
    assert params.max_tokens is None


def test_job_params_accepts_temperature_for_provider_specific_validation():
    # The schema stays provider-neutral; GPT-5 rejection happens in the OpenAI
    # adapter, while future/other providers may allow their own ranges.
    assert JobParams(temperature=1.0).temperature == 1.0


def test_job_requires_core_fields():
    with pytest.raises(ValidationError):
        Job(kind="classify", model="x", messages=[])  # missing `project`


def test_job_round_trips_through_dict():
    job = _minimal_job(meta={"comment_id": "42"})
    assert Job.model_validate(job.model_dump()) == job


def test_job_round_trips_through_wire_json_with_submitted_at():
    submitted_at = datetime(2026, 7, 3, 12, 34, 56, tzinfo=timezone.utc)
    job = _minimal_job(
        job_id=f"urn:uuid:{_JOB_ID}",
        submitted_at=submitted_at,
        callback_url=None,
    )

    wire_json = job.model_dump_json(by_alias=True)
    wire = json.loads(wire_json)

    assert job.job_id == _JOB_ID
    assert wire["job_id"] == _JOB_ID
    assert wire["submitted_at"] == "2026-07-03T12:34:56Z"
    assert wire["params"] == {
        "temperature": None,
        "max_tokens": None,
    }
    assert Job.model_validate_json(wire_json) == job


def test_job_meta_is_preserved_untouched():
    meta = {
        "comment_id": "42",
        "nested": {"a": 1},
        "provider_hints": {"extra": {"arbitrary": "payload"}},
        "labels": ["spam", "review"],
        "flagged": False,
        "score": 0.25,
        "empty": None,
    }
    job = _minimal_job(meta=meta)
    assert job.meta == meta
    assert Job.model_validate_json(job.model_dump_json()).meta == meta


def test_job_rejects_missing_message_content():
    with pytest.raises(ValidationError):
        _minimal_job(messages=[{"role": "user"}])


def test_job_rejects_missing_message_role():
    with pytest.raises(ValidationError):
        _minimal_job(messages=[{"content": "hi"}])


# --- Message -----------------------------------------------------------------


def test_message_rejects_unknown_role():
    with pytest.raises(ValidationError):
        Message(role="robot", content="x")


@pytest.mark.parametrize("role", ["System", "USER", "tool"])
def test_message_role_validation_is_case_sensitive(role):
    with pytest.raises(ValidationError):
        Message(role=role, content="x")


# --- Usage (wire aliases in/out) ---------------------------------------------


def test_usage_serializes_with_wire_aliases():
    dumped = Usage(input_tokens=10, output_tokens=20, cost_usd=0.001).model_dump(by_alias=True)
    assert dumped == {"in": 10, "out": 20, "cost_usd": 0.001}


def test_usage_parses_from_wire_aliases():
    usage = Usage.model_validate({"in": 5, "out": 7, "cost_usd": 0.02})
    assert usage.input_tokens == 5
    assert usage.output_tokens == 7


def test_usage_parses_from_python_field_names():
    usage = Usage.model_validate({"input_tokens": 5, "output_tokens": 7, "cost_usd": 0.02})
    assert usage.input_tokens == 5
    assert usage.output_tokens == 7
    assert usage.model_dump(by_alias=True) == {"in": 5, "out": 7, "cost_usd": 0.02}


def test_usage_rejects_both_alias_and_field_name_for_same_field():
    # Under extra="forbid", supplying both the wire alias and the Python field
    # name for one field is ambiguous input and is rejected outright.
    with pytest.raises(ValidationError):
        Usage.model_validate(
            {"in": 5, "input_tokens": 999, "out": 7, "output_tokens": 888, "cost_usd": 0.02}
        )


@pytest.mark.parametrize(
    "payload",
    [
        {"in": 5, "input_tokens": 999, "out": 7, "cost_usd": 0.02},
        {"in": 5, "out": 7, "output_tokens": 888, "cost_usd": 0.02},
    ],
)
def test_usage_rejects_partial_alias_and_field_name_conflicts(payload):
    with pytest.raises(ValidationError):
        Usage.model_validate(payload)


def test_usage_round_trips_through_wire_json_with_keyword_aliases():
    usage = Usage(input_tokens=123, output_tokens=456, cost_usd=0.0789)
    wire_json = usage.model_dump_json(by_alias=True)

    assert json.loads(wire_json) == {"in": 123, "out": 456, "cost_usd": 0.0789}
    assert Usage.model_validate_json(wire_json) == usage


def test_usage_defaults_to_zero():
    usage = Usage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.cost_usd == 0.0


def test_usage_is_frozen():
    # Immutable so a completed job's token/cost record can't drift after the fact,
    # and so the provider "never price" guard (§7) can't be bypassed by mutating
    # cost_usd post-construction.
    usage = Usage(input_tokens=1, output_tokens=2)
    with pytest.raises(ValidationError):
        usage.cost_usd = 0.5


# --- Result ------------------------------------------------------------------


def test_result_status_must_be_ok_or_error():
    with pytest.raises(ValidationError):
        Result(job_id=_JOB_ID, status="pending")


def test_result_ok_defaults():
    result = Result(job_id=_JOB_ID, status="ok", completion="done")
    assert result.usage == Usage()
    assert result.error is None
    assert result.meta == {}


def test_result_default_meta_is_not_shared():
    # meta is a mutable dict, so each Result must get its own default instance.
    # (Usage is frozen now, so its default is safe to share and needs no guard.)
    first = Result(job_id=_JOB_ID, status="ok")
    second = Result(job_id=_JOB_ID_2, status="ok")

    first.meta["comment_id"] = "42"

    assert second.meta == {}
    assert second.usage == Usage()


def test_result_error_carries_message_and_meta():
    meta = {"comment_id": "9", "nested": {"reason": "toxicity"}, "attempts": [1, 2]}
    result = Result(job_id=_JOB_ID, status="error", error="boom", meta=meta)
    assert result.completion is None
    assert result.error == "boom"
    assert result.meta == meta


def test_result_round_trips_through_callback_wire_json_with_usage_aliases():
    result = Result(
        job_id=f"{{{_JOB_ID.upper()}}}",
        status="ok",
        completion="done",
        usage=Usage(input_tokens=10, output_tokens=20, cost_usd=0.003),
        provider="openai",
        meta={"comment_id": "42"},
    )

    wire_json = result.model_dump_json(by_alias=True)
    wire = json.loads(wire_json)

    assert result.job_id == _JOB_ID
    assert wire == {
        "job_id": _JOB_ID,
        "status": "ok",
        "completion": "done",
        "usage": {"in": 10, "out": 20, "cost_usd": 0.003},
        "provider": "openai",
        "error": None,
        "meta": {"comment_id": "42"},
    }
    assert Result.model_validate_json(wire_json) == result


def test_result_parses_wire_usage_aliases_from_callback_payload():
    result = Result.model_validate(
        {
            "job_id": _JOB_ID,
            "status": "ok",
            "completion": "done",
            "usage": {"in": 10, "out": 20, "cost_usd": 0.003},
            "provider": "openai",
            "error": None,
            "meta": {"comment_id": "42"},
        }
    )
    assert result.usage.input_tokens == 10
    assert result.usage.output_tokens == 20


def test_result_rejects_status_with_wrong_case():
    with pytest.raises(ValidationError):
        Result(job_id=_JOB_ID, status="OK")


# --- Contract strictness (extra=forbid, uuid job_id, max_tokens > 0) ---------


def test_job_rejects_unknown_field():
    # A producer typo like `callback` for `callback_url` must fail loudly, not
    # be silently dropped (ARCHITECTURE.md §4).
    with pytest.raises(ValidationError):
        _minimal_job(callback="http://x/cb")


def test_job_rejects_unknown_field_in_nested_message():
    with pytest.raises(ValidationError):
        _minimal_job(messages=[{"role": "user", "content": "hi", "name": "operator"}])


def test_job_rejects_unknown_field_in_nested_params():
    with pytest.raises(ValidationError):
        _minimal_job(params={"temperature": 0.5, "timeout_seconds": 30})


def test_meta_remains_free_form_while_contract_models_forbid_extras():
    meta = {
        "callback": "not the contract callback_url",
        "params": {"timeout_seconds": 30},
        "messages": [{"role": "tool", "content": "kept as metadata"}],
    }
    assert _minimal_job(meta=meta).meta == meta
    assert Result(job_id=_JOB_ID, status="ok", meta=meta).meta == meta


def test_result_rejects_unknown_field():
    with pytest.raises(ValidationError):
        Result(job_id=_JOB_ID, status="ok", complete="done")


def test_result_rejects_unknown_field_in_nested_usage():
    with pytest.raises(ValidationError):
        Result(job_id=_JOB_ID, status="ok", usage={"in": 1, "out": 2, "total": 3})


def test_job_params_reject_unknown_field():
    with pytest.raises(ValidationError):
        JobParams(temperatur=0.5)


def test_usage_rejects_unknown_field():
    with pytest.raises(ValidationError):
        Usage.model_validate({"in": 1, "out": 2, "cost_usd": 0.0, "total": 3})


@pytest.mark.parametrize("bad_id", ["", "j", "not-a-uuid", "12345"])
def test_job_rejects_non_uuid_job_id(bad_id):
    with pytest.raises(ValidationError):
        _minimal_job(job_id=bad_id)


@pytest.mark.parametrize("bad_id", ["", "j", "not-a-uuid"])
def test_result_rejects_non_uuid_job_id(bad_id):
    with pytest.raises(ValidationError):
        Result(job_id=bad_id, status="ok")


def test_job_accepts_supplied_valid_uuid():
    assert _minimal_job(job_id=_JOB_ID).job_id == _JOB_ID


@pytest.mark.parametrize(
    "job_id",
    [
        _JOB_ID.upper(),
        f"urn:uuid:{_JOB_ID}",
        f"{{{_JOB_ID}}}",
        f"{{{_JOB_ID.upper()}}}",
        _JOB_ID.replace("-", ""),
    ],
)
def test_job_id_is_stored_as_canonical_uuid_string(job_id):
    # The same logical UUID must not produce different store keys / callback ids.
    assert _minimal_job(job_id=job_id).job_id == _JOB_ID
    assert Result(job_id=job_id, status="ok").job_id == _JOB_ID


def test_job_id_normalizes_to_same_store_key_from_json_payloads():
    payload = _minimal_job(job_id=_JOB_ID.upper()).model_dump_json()
    assert Job.model_validate_json(payload).job_id == _JOB_ID

    result_payload = Result(job_id=f"urn:uuid:{_JOB_ID}", status="ok").model_dump_json()
    assert Result.model_validate_json(result_payload).job_id == _JOB_ID


def test_job_id_normalizes_to_same_store_key_from_model_validate_payloads():
    job_payload = {
        "job_id": _JOB_ID.replace("-", ""),
        "project": "hate-moderator",
        "kind": "classify",
        "model": "gpt-5-mini",
        "messages": [{"role": "user", "content": "hi"}],
    }
    assert Job.model_validate(job_payload).job_id == _JOB_ID

    result_payload = {"job_id": f"{{{_JOB_ID.upper()}}}", "status": "ok"}
    assert Result.model_validate(result_payload).job_id == _JOB_ID


@pytest.mark.parametrize("bad_id", [f" {_JOB_ID}", f"{_JOB_ID} "])
def test_job_id_rejects_whitespace_padded_uuid(bad_id):
    with pytest.raises(ValidationError):
        _minimal_job(job_id=bad_id)
    with pytest.raises(ValidationError):
        Result(job_id=bad_id, status="ok")


@pytest.mark.parametrize(
    "bad_id",
    [
        uuid.UUID(_JOB_ID),
        _JOB_ID.encode(),
        bytearray(_JOB_ID.encode()),
        123,
        None,
    ],
)
def test_job_id_rejects_non_string_inputs(bad_id):
    with pytest.raises(ValidationError):
        _minimal_job(job_id=bad_id)
    with pytest.raises(ValidationError):
        Result(job_id=bad_id, status="ok")


@pytest.mark.parametrize("bad_id", [_JOB_ID.encode(), 123, None])
def test_job_id_rejects_non_string_inputs_from_model_validate(bad_id):
    job_payload = {
        "job_id": bad_id,
        "project": "hate-moderator",
        "kind": "classify",
        "model": "gpt-5-mini",
        "messages": [{"role": "user", "content": "hi"}],
    }
    with pytest.raises(ValidationError):
        Job.model_validate(job_payload)
    with pytest.raises(ValidationError):
        Result.model_validate({"job_id": bad_id, "status": "ok"})


@pytest.mark.parametrize("bad_id", [123, None])
def test_job_id_rejects_non_string_inputs_from_json_payloads(bad_id):
    job_payload = {
        "job_id": bad_id,
        "project": "hate-moderator",
        "kind": "classify",
        "model": "gpt-5-mini",
        "messages": [{"role": "user", "content": "hi"}],
    }
    with pytest.raises(ValidationError):
        Job.model_validate_json(json.dumps(job_payload))
    with pytest.raises(ValidationError):
        Result.model_validate_json(json.dumps({"job_id": bad_id, "status": "ok"}))


@pytest.mark.parametrize("bad_max_tokens", [0, -1, -100])
def test_job_params_reject_non_positive_max_tokens(bad_max_tokens):
    with pytest.raises(ValidationError):
        JobParams(max_tokens=bad_max_tokens)


def test_job_params_accept_positive_max_tokens():
    assert JobParams(max_tokens=1).max_tokens == 1
