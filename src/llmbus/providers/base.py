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
    # The hate-moderator pilot's classifier model (§14 #6).
    "gpt-5.4-mini": "openai",
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


def _reject_priced_usage(usage: Usage) -> None:
    """Enforce the "providers never price" contract (§6/§7).

    Providers report tokens only; `cost.py` is the sole pricing authority (dated
    table). A non-zero `cost_usd` reaching a `ProviderResult` is a contract
    violation, not data we'd trust, so reject it loudly rather than let it shadow
    the real computed cost downstream. Kept a module-level function (not inlined
    in `__post_init__`) so the mutation gate reaches it — mutmut does not mutate
    methods of `@dataclass` classes (same reason `ratelimit._require_non_negative`
    is extracted).
    """
    if usage.cost_usd != 0.0:
        raise ValueError(
            "providers must not price a result; leave usage.cost_usd at its "
            f"0.0 default (cost.py fills it, §6), got {usage.cost_usd!r}"
        )


@dataclasses.dataclass(frozen=True)
class ProviderResult:
    """A provider's raw output for one call: the completion text and token usage.

    `usage.cost_usd` must stay at its 0.0 default — providers report tokens only;
    `cost.py` prices downstream (§6). A non-zero cost is rejected in
    `__post_init__` via `_reject_priced_usage`.
    """

    completion: str
    usage: Usage

    def __post_init__(self) -> None:
        _reject_priced_usage(self.usage)


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
