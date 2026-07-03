"""OpenAI provider adapter (ARCHITECTURE.md §7).

Implements the `Provider` contract for the GPT-5 family via the OpenAI Chat
Completions API. The SDK client is **injected** (`config.py` wires the real
`AsyncOpenAI`; tests inject a fake), so this module imports nothing from the SDK
and stays pure enough for the mutation gate. Request building and usage
normalization are module-level functions for the same reason.

GPT-5 specifics (verified July 2026, §14 #9): the whole family rejects any
`temperature` other than its fixed default (a 400), and takes
`max_completion_tokens`, not `max_tokens`. So this adapter maps
`max_tokens -> max_completion_tokens` and rejects a job that sets a temperature
the model won't honor — fail-loud, before the API call (§4).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from llmbus.providers.base import ProviderResult
from llmbus.schema import JobParams, Message, Usage


def _openai_messages(messages: Sequence[Message]) -> list[dict[str, str]]:
    return [{"role": m.role, "content": m.content} for m in messages]


def _openai_request(model: str, messages: Sequence[Message], params: JobParams) -> dict[str, Any]:
    """Build kwargs for `chat.completions.create`, enforcing GPT-5's param rules."""
    if params.temperature is not None:
        raise ValueError(
            f"model {model!r} does not support a caller-set temperature; the "
            "GPT-5 family only uses its fixed default, so leave temperature unset"
        )
    request: dict[str, Any] = {"model": model, "messages": _openai_messages(messages)}
    if params.max_tokens is not None:
        request["max_completion_tokens"] = params.max_tokens
    return request


def _completion_from_response(response: Any) -> str:
    """Extract the completion text, rejecting a missing one (fail-loud)."""
    content: str | None = response.choices[0].message.content
    if content is None:
        raise ValueError("OpenAI response carried no completion content")
    return content


def _usage_from_openai(usage: Any) -> Usage:
    """Normalize OpenAI usage to our `Usage` (tokens only; `cost_usd` stays 0.0).

    `completion_tokens` already includes GPT-5 reasoning tokens, so it maps to
    `output_tokens` and prices correctly downstream (`cost.py`).
    """
    return Usage(input_tokens=usage.prompt_tokens, output_tokens=usage.completion_tokens)


class OpenAIAdapter:
    """`Provider` for the OpenAI GPT-5 family, over an injected async client."""

    name = "openai"

    def __init__(self, client: Any) -> None:
        # `AsyncOpenAI`-shaped; injected so tests use a fake and config wires the
        # real one. Typed `Any` to keep this module free of a hard SDK import.
        self._client = client

    async def call(
        self, model: str, messages: Sequence[Message], params: JobParams
    ) -> ProviderResult:
        response = await self._client.chat.completions.create(
            **_openai_request(model, messages, params)
        )
        return ProviderResult(
            completion=_completion_from_response(response),
            usage=_usage_from_openai(response.usage),
        )
