"""Anthropic provider adapter (ARCHITECTURE.md §7).

Implements the `Provider` contract for the Claude family via the Anthropic
Messages API. The SDK client is **injected** (`config.py` wires the real
`AsyncAnthropic`; tests inject a fake), so this module imports nothing from the
SDK and stays pure logic (in the mutation gate). Request building and usage
normalization are module-level functions for the same reason.

Anthropic specifics (verified against the Messages API, July 2026) — these are
why this is a separate adapter, not a shared class with the OpenAI one:

- `max_tokens` is **required** (OpenAI's is optional) — a job that omits it is
  rejected before the call.
- The system prompt is a **separate top-level `system=` param**, not a message,
  so system-role messages are split out of `messages`.
- `temperature` support is **per-model**: the current top models
  (claude-opus-4-8, claude-sonnet-5) reject any caller-set temperature (400);
  claude-haiku-4-5 accepts 0.0-1.0. So temperature is validated per model (§7).
- `response.content` is a list of blocks; the first `text` block is the
  completion. `usage.input_tokens`/`output_tokens` map straight to our `Usage`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from llmbus.providers.base import ProviderResult
from llmbus.schema import JobParams, Message, Usage

# Claude models that accept a caller-set `temperature` (range 0.0-1.0). The
# current top models reject it with a 400; per Anthropic's docs only Haiku does
# (it is not in the "sampling params removed" set). Live integration tests
# (config PR) must confirm this; if Haiku also rejects it, drop it from the set.
_MODELS_SUPPORTING_TEMPERATURE: frozenset[str] = frozenset({"claude-haiku-4-5"})


def _anthropic_system_and_messages(
    messages: Sequence[Message],
) -> tuple[str | None, list[dict[str, str]]]:
    """Split our messages into Anthropic's separate `system` string + chat list."""
    system_parts = [m.content for m in messages if m.role == "system"]
    chat = [{"role": m.role, "content": m.content} for m in messages if m.role != "system"]
    system = "\n\n".join(system_parts) if system_parts else None
    return system, chat


def _anthropic_temperature(model: str, params: JobParams) -> float | None:
    """Validate `temperature` for `model`; return the value to forward, or None."""
    if params.temperature is None:
        return None
    if model not in _MODELS_SUPPORTING_TEMPERATURE:
        raise ValueError(
            f"model {model!r} does not support a caller-set temperature; leave it unset"
        )
    if not 0.0 <= params.temperature <= 1.0:
        raise ValueError(
            f"temperature {params.temperature!r} is out of range for {model!r}; "
            "Anthropic accepts 0.0-1.0"
        )
    return params.temperature


def _anthropic_request(
    model: str, messages: Sequence[Message], params: JobParams
) -> dict[str, Any]:
    """Build kwargs for `messages.create`, enforcing Anthropic's contract."""
    if params.max_tokens is None:
        raise ValueError(
            f"Anthropic requires max_tokens; set params.max_tokens for model {model!r}"
        )
    system, chat = _anthropic_system_and_messages(messages)
    request: dict[str, Any] = {"model": model, "max_tokens": params.max_tokens, "messages": chat}
    if system is not None:
        request["system"] = system
    temperature = _anthropic_temperature(model, params)
    if temperature is not None:
        request["temperature"] = temperature
    return request


def _completion_from_response(response: Any) -> str:
    """Return the first text block's text, rejecting a response that has none."""
    for block in response.content:
        if block.type == "text":
            text: str = block.text
            return text
    raise ValueError("Anthropic response carried no text block")


def _usage_from_anthropic(usage: Any) -> Usage:
    """Normalize Anthropic usage to our `Usage` (tokens only; `cost_usd` stays 0.0)."""
    return Usage(input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)


class AnthropicAdapter:
    """`Provider` for the Anthropic Claude family, over an injected async client."""

    name = "anthropic"

    def __init__(self, client: Any) -> None:
        # `AsyncAnthropic`-shaped; injected so tests use a fake and config wires
        # the real one. Typed `Any` to keep this module free of a hard SDK import.
        self._client = client

    async def call(
        self, model: str, messages: Sequence[Message], params: JobParams
    ) -> ProviderResult:
        response = await self._client.messages.create(**_anthropic_request(model, messages, params))
        return ProviderResult(
            completion=_completion_from_response(response),
            usage=_usage_from_anthropic(response.usage),
        )
