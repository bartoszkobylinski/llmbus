"""Unit tests for the message contract (ARCHITECTURE.md §4)."""

import json
import uuid
from datetime import datetime, timedelta, timezone, tzinfo
from math import inf, nextafter

import pytest
from pydantic import ValidationError

from llmbus.schema import (
    MAX_TTL_S,
    Job,
    JobParams,
    Message,
    ModelPolicyError,
    ResponseFormat,
    Result,
    Usage,
    resolve_model,
)

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
    assert job.ttl_s is None


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
        ttl_s=280.0,
    )

    wire_json = job.model_dump_json(by_alias=True)
    wire = json.loads(wire_json)

    assert job.job_id == _JOB_ID
    assert wire["job_id"] == _JOB_ID
    assert wire["submitted_at"] == "2026-07-03T12:34:56Z"
    assert wire["ttl_s"] == 280.0
    assert wire["params"] == {
        "temperature": None,
        "max_tokens": None,
        "response_format": None,
    }
    assert Job.model_validate_json(wire_json) == job


@pytest.mark.parametrize("ttl_s", [0, -0.001, -1])
def test_job_rejects_a_non_positive_ttl(ttl_s):
    with pytest.raises(ValidationError):
        _minimal_job(ttl_s=ttl_s)


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


def test_job_params_reject_response_format_v1_contract():
    with pytest.raises(ValidationError):
        JobParams(response_format="json_object")


def test_job_rejects_response_format_in_nested_params_v1_contract():
    with pytest.raises(ValidationError):
        _minimal_job(params={"response_format": "json_object"})


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


# --- ResponseFormat (§14 #10: json_schema only) ------------------------------

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"category": {"type": "string"}, "confidence": {"type": "number"}},
    "required": ["category", "confidence"],
    "additionalProperties": False,
}


def test_response_format_defaults_type_to_json_schema():
    fmt = ResponseFormat(name="verdict", json_schema=_VERDICT_SCHEMA)
    assert fmt.type == "json_schema"
    assert fmt.name == "verdict"
    assert fmt.json_schema == _VERDICT_SCHEMA


def test_response_format_rejects_other_types():
    with pytest.raises(ValidationError):
        ResponseFormat(type="json_object", name="verdict", json_schema=_VERDICT_SCHEMA)


def test_response_format_accepts_single_char_name():
    assert ResponseFormat(name="v", json_schema=_VERDICT_SCHEMA).name == "v"


def test_response_format_rejects_empty_name():
    with pytest.raises(ValidationError):
        ResponseFormat(name="", json_schema=_VERDICT_SCHEMA)


def test_response_format_requires_name_and_schema():
    with pytest.raises(ValidationError):
        ResponseFormat(name="verdict")
    with pytest.raises(ValidationError):
        ResponseFormat(json_schema=_VERDICT_SCHEMA)


def test_response_format_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ResponseFormat(name="verdict", json_schema=_VERDICT_SCHEMA, strict=True)


# The three rejection tests assert the pydantic error `msg` by EXACT equality,
# not `match=` — `match` is a substring search, which a mutated message string
# still satisfies (see the mutmut-skips-dataclass-methods note: full-message
# assertions are what pin error text in the mutation gate).


def _sole_error_msg(exc_info) -> str:
    errors = exc_info.value.errors()
    assert len(errors) == 1
    return errors[0]["msg"]


def test_response_format_rejects_empty_schema():
    with pytest.raises(ValidationError) as exc_info:
        ResponseFormat(name="verdict", json_schema={})
    assert (
        _sole_error_msg(exc_info)
        == "Value error, response_format schema must be a non-empty JSON Schema object"
    )


@pytest.mark.parametrize("top_type", ["array", "string", None])
def test_response_format_rejects_non_object_top_level(top_type):
    schema = {"type": top_type, "additionalProperties": False}
    if top_type is None:
        del schema["type"]
    with pytest.raises(ValidationError) as exc_info:
        ResponseFormat(name="verdict", json_schema=schema)
    assert (
        _sole_error_msg(exc_info)
        == "Value error, response_format schema must declare top-level type 'object'"
    )


@pytest.mark.parametrize("extra_props", [True, {}, None])
def test_response_format_rejects_loose_additional_properties(extra_props):
    schema = {"type": "object", "additionalProperties": extra_props}
    if extra_props is None:
        del schema["additionalProperties"]
    with pytest.raises(ValidationError) as exc_info:
        ResponseFormat(name="verdict", json_schema=schema)
    assert (
        _sole_error_msg(exc_info)
        == "Value error, response_format schema must set top-level additionalProperties to false"
    )


def test_response_format_serializes_schema_under_wire_alias():
    fmt = ResponseFormat(name="verdict", json_schema=_VERDICT_SCHEMA)
    wire = fmt.model_dump(by_alias=True)
    assert wire == {"type": "json_schema", "name": "verdict", "schema": _VERDICT_SCHEMA}
    assert "json_schema" not in wire


def test_response_format_parses_from_wire_alias():
    fmt = ResponseFormat.model_validate(
        {"type": "json_schema", "name": "verdict", "schema": _VERDICT_SCHEMA}
    )
    assert fmt.json_schema == _VERDICT_SCHEMA


def test_response_format_rejects_alias_and_field_name_together():
    with pytest.raises(ValidationError) as exc_info:
        ResponseFormat.model_validate(
            {
                "name": "verdict",
                "schema": _VERDICT_SCHEMA,
                "json_schema": _VERDICT_SCHEMA,
            }
        )
    assert exc_info.value.errors()[0]["loc"] == ("json_schema",)
    assert exc_info.value.errors()[0]["type"] == "extra_forbidden"


def test_response_format_accepts_schema_keywords_and_only_checks_top_level_strictness():
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {"detail": {"type": "object"}},
        "type": "object",
        "properties": {
            "anything": {},
            "detail": {
                "type": "object",
                "properties": {"note": {"type": ["string", "null"]}},
            },
        },
        "additionalProperties": False,
    }

    assert ResponseFormat(name="verdict", json_schema=schema).json_schema == schema


def test_response_format_requires_json_false_not_an_equal_integer():
    with pytest.raises(ValidationError) as exc_info:
        ResponseFormat(
            name="verdict",
            json_schema={"type": "object", "additionalProperties": 0},
        )
    assert (
        _sole_error_msg(exc_info)
        == "Value error, response_format schema must set top-level additionalProperties to false"
    )


def test_job_params_response_format_defaults_to_none():
    assert JobParams().response_format is None


def test_job_round_trips_response_format_through_wire_json():
    job = _minimal_job(
        params={
            "max_tokens": 128,
            "response_format": {
                "type": "json_schema",
                "name": "verdict",
                "schema": _VERDICT_SCHEMA,
            },
        }
    )
    wire = json.loads(job.model_dump_json(by_alias=True))
    assert wire["params"]["response_format"]["schema"] == _VERDICT_SCHEMA
    assert "json_schema" not in wire["params"]["response_format"]
    assert Job.model_validate_json(job.model_dump_json(by_alias=True)) == job


# --- deadline validation (§14 #22) -------------------------------------------


def _job_kwargs(**overrides):
    data = {
        "project": "hate-moderator",
        "kind": "classify",
        "model": "gpt-5-nano",
        "messages": [Message(role="user", content="hi")],
    }
    data.update(overrides)
    return data


def test_a_naive_submitted_at_is_rejected_at_the_contract_boundary():
    """The worker compares this against its own aware clock; a naive value would
    raise TypeError deep in the job path, after the job was already queued."""
    # Anchored on the full message: a substring match would still pass if the
    # text were mangled, and this string is what tells the producer what to fix.
    with pytest.raises(ValidationError, match=r"Value error, submitted_at must be timezone-aware"):
        Job(**_job_kwargs(submitted_at=datetime(2026, 1, 1)))


class _OffsetOnlyForRealDatetimes(tzinfo):
    """Aware, but only answers when asked about an actual datetime.

    `tzinfo.utcoffset` takes the datetime as its argument, and a real
    implementation may need it (DST rules do). Asking with None instead would
    call such a zone "naive" and reject a perfectly valid timestamp.
    """

    def utcoffset(self, dt):
        return None if dt is None else timedelta(hours=2)

    def tzname(self, dt):
        return "REAL-ONLY"

    def dst(self, dt):
        return timedelta(0)


class _NeverHasAnOffset(tzinfo):
    """Carries a tzinfo but yields no offset — aware in name only."""

    def utcoffset(self, dt):
        return None

    def tzname(self, dt):
        return "PSEUDO"

    def dst(self, dt):
        return None


def test_a_zone_that_needs_the_datetime_is_still_accepted():
    stamped = datetime(2026, 1, 1, tzinfo=_OffsetOnlyForRealDatetimes())
    assert Job(**_job_kwargs(submitted_at=stamped)).submitted_at == stamped


def test_a_tzinfo_that_yields_no_offset_is_rejected():
    """`tzinfo is not None` alone is not awareness — this is why the check asks
    for the offset rather than just the attribute."""
    with pytest.raises(ValidationError, match=r"Value error, submitted_at must be timezone-aware"):
        Job(**_job_kwargs(submitted_at=datetime(2026, 1, 1, tzinfo=_NeverHasAnOffset())))


def test_an_aware_submitted_at_is_accepted():
    stamped = datetime(2026, 1, 1, tzinfo=timezone.utc)
    assert Job(**_job_kwargs(submitted_at=stamped)).submitted_at == stamped


def test_an_infinite_ttl_is_rejected():
    """The nastiest of the invalid values: inf passes a naive `> 0` check and
    serialises to JSON null, so the deadline LOOKS set and silently does nothing."""
    with pytest.raises(ValidationError):
        Job(**_job_kwargs(ttl_s=float("inf")))


def test_a_nan_ttl_is_rejected():
    with pytest.raises(ValidationError):
        Job(**_job_kwargs(ttl_s=float("nan")))


def test_a_ttl_beyond_timedeltas_range_is_rejected():
    """A huge finite TTL would raise OverflowError at the expiry comparison."""
    with pytest.raises(ValidationError):
        Job(**_job_kwargs(ttl_s=1e30))


@pytest.mark.parametrize("ttl_s", [0, -1])
def test_a_non_positive_ttl_is_rejected(ttl_s):
    with pytest.raises(ValidationError):
        Job(**_job_kwargs(ttl_s=ttl_s))


def test_the_maximum_ttl_is_accepted_and_a_hair_over_is_not():
    assert Job(**_job_kwargs(ttl_s=MAX_TTL_S)).ttl_s == MAX_TTL_S
    assert Job(**_job_kwargs(ttl_s=nextafter(MAX_TTL_S, 0.0))).ttl_s < MAX_TTL_S
    with pytest.raises(ValidationError):
        Job(**_job_kwargs(ttl_s=nextafter(MAX_TTL_S, inf)))


def test_no_ttl_means_no_deadline():
    assert Job(**_job_kwargs()).ttl_s is None


# --- resolve_model / central model policy (§14 #23) --------------------------


def _policy_job(**overrides):
    data = {
        "project": "milamber",
        "kind": "series_classify",
        "messages": [Message(role="user", content="hi")],
    }
    data.update(overrides)
    return Job(**data)


def test_job_model_defaults_to_none_meaning_the_bus_decides():
    assert _policy_job().model is None


def test_resolve_model_uses_the_policy_when_the_job_named_no_model():
    assert resolve_model(_policy_job(), "gpt-5.4") == "gpt-5.4"


def test_resolve_model_keeps_an_explicit_model_and_ignores_the_policy():
    # Pinning must survive a policy change, or a producer that deliberately chose
    # a model would silently drift the next time someone edits the table.
    assert resolve_model(_policy_job(model="gpt-5-nano"), "gpt-5.4") == "gpt-5-nano"


def test_resolve_model_keeps_an_explicit_model_when_there_is_no_policy_at_all():
    assert resolve_model(_policy_job(model="gpt-5-nano"), None) == "gpt-5-nano"


def test_resolve_model_hard_fails_when_neither_the_job_nor_a_policy_names_one():
    # Deliberately not a fallback to some default: a silent default is exactly the
    # drift §14 #23 exists to remove.
    with pytest.raises(ModelPolicyError) as caught:
        resolve_model(_policy_job(), None)
    assert str(caught.value) == (
        "no model policy for project 'milamber' kind 'series_classify', and the job set no model"
    )


def test_resolve_model_error_names_the_pair_that_was_missing():
    with pytest.raises(ModelPolicyError) as caught:
        resolve_model(_policy_job(project="hate-moderator", kind="classify"), None)
    assert "'hate-moderator'" in str(caught.value)
    assert "'classify'" in str(caught.value)


def test_model_policy_error_is_a_value_error():
    # Producers already catch ValueError around submit validation; this joins it
    # rather than inventing a parallel hierarchy.
    assert issubclass(ModelPolicyError, ValueError)


def test_a_job_may_still_be_constructed_with_an_explicit_model():
    assert _policy_job(model="claude-haiku-4-5").model == "claude-haiku-4-5"
