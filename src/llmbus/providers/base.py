"""Provider abstraction: the call contract, model→provider routing, and the
normalized result shape (ARCHITECTURE.md §7).

This module is pure logic — a routing table and type contracts, no network. The
concrete adapters (`openai.py`, `anthropic.py`) that actually call the SDKs live
alongside it and are covered by integration tests, not here.

A model is served by exactly one provider. `PROVIDERS` is the single source of
truth for that routing, and a test keeps it in lockstep with `cost.PRICING` — so a
model can never be priced without a route, nor routed without a price. Adding a
third provider later (OpenRouter, §7) is a new table entry plus an adapter.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from llmbus.schema import JobParams, Message, Usage


class UnknownModelError(KeyError):
    """No provider serves this model — raised rather than guessing a route."""


# model → provider name. Keep in sync with `cost.PRICING` (a test enforces it).
PROVIDERS: dict[str, str] = {
    # OpenAI — GPT-5 family
    "gpt-5": "openai",
    "gpt-5-mini": "openai",
    "gpt-5-nano": "openai",
    # Anthropic
    "claude-opus-4-8": "anthropic",
    "claude-sonnet-5": "anthropic",
    "claude-haiku-4-5": "anthropic",
}


def provider_for(model: str) -> str:
    """Name of the provider that serves `model`, or raise `UnknownModelError`."""
    try:
        return PROVIDERS[model]
    except KeyError:
        raise UnknownModelError(model) from None


@dataclasses.dataclass(frozen=True)
class ProviderResult:
    """A provider's raw output for one call: the completion text and token usage.

    `usage.cost_usd` is left at its 0.0 default — cost is applied downstream from
    `cost.py`, so providers stay unaware of pricing (§7).
    """

    completion: str
    usage: Usage


@runtime_checkable
class Provider(Protocol):
    """The contract every provider adapter implements.

    `name` matches the routing value in `PROVIDERS` (e.g. "openai"); `call` runs
    one model request and returns a normalized `ProviderResult`. Adapters own
    their own SDK/usage-shape normalization so the worker sees one shape.
    """

    name: str

    async def call(
        self, model: str, messages: Sequence[Message], params: JobParams
    ) -> ProviderResult: ...
