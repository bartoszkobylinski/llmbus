"""Unit tests for the Anthropic adapter (§7).

The SDK client is injected, so these mock it — no network, no API key. We assert
the request we build and the ProviderResult we normalize, not the SDK's behavior.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llmbus.providers.anthropic import (
    AnthropicAdapter,
    _anthropic_request,
    _anthropic_system_and_messages,
    _anthropic_temperature,
    _completion_from_response,
    _usage_from_anthropic,
)
from llmbus.providers.base import Provider, ProviderResult
from llmbus.schema import JobParams, Message, ResponseFormat, Usage

_USER = [Message(role="user", content="hi")]

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {"category": {"type": "string"}},
    "required": ["category"],
    "additionalProperties": False,
}


def _response(text="ok", blocks=None, stop_reason="end_turn", usage=(3, 5)):
    if blocks is None:
        blocks = [SimpleNamespace(type="text", text=text)]
    input_tokens, output_tokens = usage
    return SimpleNamespace(
        content=blocks,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
    )


def _client(response):
    return SimpleNamespace(messages=SimpleNamespace(create=AsyncMock(return_value=response)))


# --- system / message split --------------------------------------------------


def test_splits_system_out_of_the_messages_list():
    system, chat = _anthropic_system_and_messages(
        [Message(role="system", content="be terse"), Message(role="user", content="hi")]
    )
    assert system == "be terse"
    assert chat == [{"role": "user", "content": "hi"}]


def test_no_system_message_yields_none():
    system, chat = _anthropic_system_and_messages(_USER)
    assert system is None
    assert chat == [{"role": "user", "content": "hi"}]


def test_multiple_system_messages_are_joined():
    system, _ = _anthropic_system_and_messages(
        [
            Message(role="system", content="a"),
            Message(role="system", content="b"),
            Message(role="user", content="hi"),
        ]
    )
    assert system == "a\n\nb"


def test_system_messages_are_removed_without_reordering_chat_messages():
    system, chat = _anthropic_system_and_messages(
        [
            Message(role="user", content="first"),
            Message(role="system", content="rules"),
            Message(role="assistant", content="second"),
            Message(role="user", content="third"),
        ]
    )

    assert system == "rules"
    assert chat == [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]


# --- max_tokens (required by Anthropic) --------------------------------------


def test_request_requires_max_tokens():
    with pytest.raises(ValueError) as exc_info:
        _anthropic_request("claude-opus-4-8", _USER, JobParams())
    assert str(exc_info.value) == (
        "Anthropic requires max_tokens; set params.max_tokens for model 'claude-opus-4-8'"
    )


def test_request_includes_max_tokens_and_messages():
    request = _anthropic_request("claude-opus-4-8", _USER, JobParams(max_tokens=256))
    assert request == {
        "model": "claude-opus-4-8",
        "max_tokens": 256,
        "messages": [{"role": "user", "content": "hi"}],
    }


def test_request_includes_system_when_present():
    request = _anthropic_request(
        "claude-opus-4-8",
        [Message(role="system", content="be terse"), Message(role="user", content="hi")],
        JobParams(max_tokens=64),
    )
    assert request["system"] == "be terse"


# --- temperature (per-model) -------------------------------------------------


def test_temperature_none_is_omitted():
    assert _anthropic_temperature("claude-haiku-4-5", JobParams(temperature=None)) is None


@pytest.mark.parametrize("model", ["claude-opus-4-8", "claude-sonnet-5"])
def test_temperature_rejected_for_models_without_support(model):
    with pytest.raises(ValueError) as exc_info:
        _anthropic_temperature(model, JobParams(temperature=0.5))
    assert str(exc_info.value) == (
        f"model {model!r} does not support a caller-set temperature; leave it unset"
    )


@pytest.mark.parametrize("temperature", [0.0, 0.5, 1.0])
def test_temperature_forwarded_within_range_for_haiku(temperature):
    assert _anthropic_temperature("claude-haiku-4-5", JobParams(temperature=temperature)) == (
        temperature
    )


@pytest.mark.parametrize("temperature", [-0.1, 1.5])
def test_temperature_out_of_range_rejected_for_haiku(temperature):
    with pytest.raises(ValueError) as exc_info:
        _anthropic_temperature("claude-haiku-4-5", JobParams(temperature=temperature))
    assert str(exc_info.value) == (
        f"temperature {temperature!r} is out of range for 'claude-haiku-4-5'; "
        "Anthropic accepts 0.0-1.0"
    )


def test_request_forwards_supported_temperature():
    request = _anthropic_request(
        "claude-haiku-4-5", _USER, JobParams(max_tokens=64, temperature=0.7)
    )
    assert request["temperature"] == 0.7


def test_request_maps_response_format_to_output_config():
    params = JobParams(
        max_tokens=64,
        response_format=ResponseFormat(name="verdict", json_schema=_VERDICT_SCHEMA),
    )
    request = _anthropic_request("claude-opus-4-8", _USER, params)
    assert request["output_config"] == {
        "format": {"type": "json_schema", "schema": _VERDICT_SCHEMA}
    }


def test_request_combines_response_format_with_system_tokens_and_temperature():
    params = JobParams(
        max_tokens=64,
        temperature=0.7,
        response_format=ResponseFormat(name="verdict", json_schema=_VERDICT_SCHEMA),
    )

    assert _anthropic_request(
        "claude-haiku-4-5",
        [Message(role="system", content="classify"), Message(role="user", content="hi")],
        params,
    ) == {
        "model": "claude-haiku-4-5",
        "max_tokens": 64,
        "messages": [{"role": "user", "content": "hi"}],
        "system": "classify",
        "temperature": 0.7,
        "output_config": {
            "format": {"type": "json_schema", "schema": _VERDICT_SCHEMA},
        },
    }


def test_request_output_config_drops_openai_only_name():
    params = JobParams(
        max_tokens=64,
        response_format=ResponseFormat(name="verdict", json_schema=_VERDICT_SCHEMA),
    )
    request = _anthropic_request("claude-opus-4-8", _USER, params)
    assert "verdict" not in repr(request)


def test_request_omits_output_config_when_response_format_unset():
    assert "output_config" not in _anthropic_request(
        "claude-opus-4-8", _USER, JobParams(max_tokens=64)
    )


def test_request_omits_temperature_when_unset():
    assert "temperature" not in _anthropic_request(
        "claude-haiku-4-5", _USER, JobParams(max_tokens=64)
    )


# --- response extraction -----------------------------------------------------


def test_completion_returns_first_text_block():
    assert _completion_from_response(_response(text="hello")) == "hello"


def test_completion_skips_non_text_blocks():
    blocks = [SimpleNamespace(type="thinking", text=""), SimpleNamespace(type="text", text="hi")]
    assert _completion_from_response(_response(blocks=blocks)) == "hi"


def test_completion_rejects_response_with_no_text_block():
    blocks = [SimpleNamespace(type="thinking", text="")]
    with pytest.raises(ValueError) as exc_info:
        _completion_from_response(_response(blocks=blocks))
    assert str(exc_info.value) == "Anthropic response carried no text block"


def test_completion_rejects_text_block_with_none_text():
    blocks = [SimpleNamespace(type="text", text=None)]
    with pytest.raises(ValueError) as exc_info:
        _completion_from_response(_response(blocks=blocks))
    assert str(exc_info.value) == "Anthropic response carried no text block"


def test_completion_allows_empty_text_block():
    assert _completion_from_response(_response(text="")) == ""


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal", "pause_turn", "tool_use"])
def test_completion_rejects_any_non_end_turn_stop(stop_reason):
    with pytest.raises(ValueError) as exc_info:
        _completion_from_response(_response(text="truncated…", stop_reason=stop_reason))
    assert str(exc_info.value) == (
        f"Anthropic response stopped early (stop_reason={stop_reason!r}, "
        "not 'end_turn') — the completion is truncated or absent; with "
        "'max_tokens', raise params.max_tokens"
    )


def test_completion_stop_reason_is_checked_before_text_blocks():
    # A truncated response with no text block must report the truncation, not the
    # missing block — the stop_reason is the root cause, the block just a symptom.
    blocks = [SimpleNamespace(type="thinking", text="")]
    with pytest.raises(ValueError, match="stop_reason='max_tokens'"):
        _completion_from_response(_response(blocks=blocks, stop_reason="max_tokens"))


def test_usage_maps_tokens_and_leaves_cost_zero():
    usage = _usage_from_anthropic(SimpleNamespace(input_tokens=7, output_tokens=11))
    assert usage == Usage(input_tokens=7, output_tokens=11)
    assert usage.cost_usd == 0.0


# --- adapter -----------------------------------------------------------------


def test_adapter_name_is_anthropic():
    assert AnthropicAdapter(_client(_response())).name == "anthropic"


def test_adapter_satisfies_provider_protocol():
    assert isinstance(AnthropicAdapter(_client(_response())), Provider)


async def test_call_builds_request_and_returns_provider_result():
    client = _client(_response(text="done", usage=(3, 5)))
    adapter = AnthropicAdapter(client)

    result = await adapter.call(
        "claude-haiku-4-5",
        [Message(role="system", content="be terse"), Message(role="user", content="hi")],
        JobParams(max_tokens=64, temperature=0.2),
    )

    client.messages.create.assert_awaited_once_with(
        model="claude-haiku-4-5",
        max_tokens=64,
        messages=[{"role": "user", "content": "hi"}],
        system="be terse",
        temperature=0.2,
    )
    assert result == ProviderResult(completion="done", usage=Usage(input_tokens=3, output_tokens=5))


async def test_call_forwards_structured_output_at_the_sdk_boundary():
    client = _client(_response(text='{"category":"neutral"}'))
    adapter = AnthropicAdapter(client)
    params = JobParams(
        max_tokens=64,
        response_format=ResponseFormat(name="provider-neutral-name", json_schema=_VERDICT_SCHEMA),
    )

    await adapter.call("claude-opus-4-8", _USER, params)

    client.messages.create.assert_awaited_once_with(
        model="claude-opus-4-8",
        max_tokens=64,
        messages=[{"role": "user", "content": "hi"}],
        output_config={
            "format": {"type": "json_schema", "schema": _VERDICT_SCHEMA},
        },
    )


async def test_call_rejects_unsupported_temperature_before_touching_the_client():
    client = _client(_response())
    adapter = AnthropicAdapter(client)

    with pytest.raises(ValueError):
        await adapter.call(
            "claude-opus-4-8",
            _USER,
            JobParams(
                max_tokens=64,
                temperature=0.5,
                response_format=ResponseFormat(name="verdict", json_schema=_VERDICT_SCHEMA),
            ),
        )

    client.messages.create.assert_not_awaited()


async def test_call_rejects_missing_max_tokens_before_touching_the_client():
    client = _client(_response())
    adapter = AnthropicAdapter(client)

    with pytest.raises(ValueError, match="requires max_tokens"):
        await adapter.call(
            "claude-opus-4-8",
            _USER,
            JobParams(response_format=ResponseFormat(name="verdict", json_schema=_VERDICT_SCHEMA)),
        )

    client.messages.create.assert_not_awaited()
