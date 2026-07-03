"""Unit tests for the OpenAI adapter (§7).

The SDK client is injected, so these mock it — no network, no API key. We assert
the request we build and the ProviderResult we normalize, not the SDK's behavior.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from llmbus.providers.base import Provider, ProviderResult
from llmbus.providers.openai import (
    OpenAIAdapter,
    _completion_from_response,
    _openai_request,
    _usage_from_openai,
)
from llmbus.schema import JobParams, Message, Usage

_MESSAGES = [Message(role="user", content="hi")]


def _response(content="ok", prompt_tokens=3, completion_tokens=5):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


def _client(response):
    completions = SimpleNamespace(create=AsyncMock(return_value=response))
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


# --- request building --------------------------------------------------------


def test_request_maps_messages_to_role_content_dicts():
    request = _openai_request(
        "gpt-5",
        [Message(role="system", content="be terse"), Message(role="user", content="hi")],
        JobParams(),
    )
    assert request == {
        "model": "gpt-5",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
    }


def test_request_maps_max_tokens_to_max_completion_tokens():
    request = _openai_request("gpt-5", _MESSAGES, JobParams(max_tokens=256))
    assert request["max_completion_tokens"] == 256
    assert "max_tokens" not in request


def test_request_omits_token_cap_when_unset():
    assert "max_completion_tokens" not in _openai_request("gpt-5", _MESSAGES, JobParams())


def test_request_includes_response_format_when_set():
    request = _openai_request("gpt-5", _MESSAGES, JobParams(response_format="json_object"))
    assert request["response_format"] == "json_object"


def test_request_omits_response_format_when_unset():
    assert "response_format" not in _openai_request("gpt-5", _MESSAGES, JobParams())


def test_request_allows_unset_temperature():
    assert "temperature" not in _openai_request("gpt-5", _MESSAGES, JobParams(temperature=None))


@pytest.mark.parametrize("temperature", [0.0, 0.5, 1.0])
def test_request_rejects_any_caller_set_temperature(temperature):
    with pytest.raises(ValueError) as exc_info:
        _openai_request("gpt-5", _MESSAGES, JobParams(temperature=temperature))
    assert str(exc_info.value) == (
        "model 'gpt-5' does not support a caller-set temperature; the "
        "GPT-5 family only uses its fixed default, so leave temperature unset"
    )


# --- response extraction -----------------------------------------------------


def test_completion_from_response_returns_content():
    assert _completion_from_response(_response(content="hello")) == "hello"


def test_completion_from_response_rejects_missing_content():
    with pytest.raises(ValueError) as exc_info:
        _completion_from_response(_response(content=None))
    assert str(exc_info.value) == "OpenAI response carried no completion content"


def test_usage_from_openai_maps_tokens_and_leaves_cost_zero():
    usage = _usage_from_openai(SimpleNamespace(prompt_tokens=7, completion_tokens=11))
    assert usage == Usage(input_tokens=7, output_tokens=11)
    assert usage.cost_usd == 0.0


# --- adapter -----------------------------------------------------------------


def test_adapter_name_is_openai():
    assert OpenAIAdapter(_client(_response())).name == "openai"


def test_adapter_satisfies_provider_protocol():
    assert isinstance(OpenAIAdapter(_client(_response())), Provider)


async def test_call_builds_request_and_returns_provider_result():
    client = _client(_response(content="done", prompt_tokens=3, completion_tokens=5))
    adapter = OpenAIAdapter(client)

    result = await adapter.call("gpt-5", _MESSAGES, JobParams(max_tokens=64))

    client.chat.completions.create.assert_awaited_once_with(
        model="gpt-5",
        messages=[{"role": "user", "content": "hi"}],
        max_completion_tokens=64,
    )
    assert result == ProviderResult(completion="done", usage=Usage(input_tokens=3, output_tokens=5))


async def test_call_rejects_temperature_before_touching_the_client():
    client = _client(_response())
    adapter = OpenAIAdapter(client)

    with pytest.raises(ValueError):
        await adapter.call("gpt-5", _MESSAGES, JobParams(temperature=0.0))

    client.chat.completions.create.assert_not_awaited()
