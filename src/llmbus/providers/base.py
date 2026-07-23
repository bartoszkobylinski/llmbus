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
from typing import Literal, Protocol, runtime_checkable

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
    # milamber's routed models (§14 #23 step 2); prices verified 2026-07-23.
    "gpt-5.2": "openai",
    "gpt-5.4": "openai",
    "gpt-5.5": "openai",
    # Anthropic
    "claude-opus-4-8": "anthropic",
    "claude-sonnet-5": "anthropic",
    "claude-haiku-4-5": "anthropic",
}


# What a model is *for*. Routing alone is not enough once the bus chooses models
# centrally (§14 #23): `PROVIDERS` says `whisper-1` is served by OpenAI, but
# nothing there says it transcribes rather than chats — so a policy row could
# point a chat task at it and the mistake would surface at the provider, or worse
# be sent as a chat call. The policy UI filters its dropdown on this, so the wrong
# pairing is not selectable in the first place.
Capability = Literal["chat", "transcription", "embedding"]

# model → what it serves. Kept in lockstep with `PROVIDERS` by a test, the same
# way `PROVIDERS` is kept in lockstep with `cost.PRICING`: a model can never be
# routed without a capability, nor declared capable without a route. Every entry
# is "chat" today; `transcription` arrives with §4 v2 (§14 #24) and `embedding`
# after it.
CAPABILITIES: dict[str, Capability] = {
    "gpt-5": "chat",
    "gpt-5-mini": "chat",
    "gpt-5-nano": "chat",
    "gpt-5.4-mini": "chat",
    "gpt-5.2": "chat",
    "gpt-5.4": "chat",
    "gpt-5.5": "chat",
    "claude-opus-4-8": "chat",
    "claude-sonnet-5": "chat",
    "claude-haiku-4-5": "chat",
}


def provider_for(model: str) -> str:
    """Name of the provider that serves `model`, or raise `UnknownModelError`."""
    try:
        return PROVIDERS[model]
    except KeyError:
        raise UnknownModelError(model) from None


def capability_for(model: str) -> Capability:
    """What `model` serves, or raise `UnknownModelError`.

    Same fail-loud shape as `provider_for`: an unregistered model has no assumed
    capability. Guessing "probably chat" is how a transcription model ends up in
    a chat call.
    """
    try:
        return CAPABILITIES[model]
    except KeyError:
        raise UnknownModelError(model) from None


def models_with_capability(capability: Capability) -> list[str]:
    """Every registered model serving `capability`, sorted.

    This is what the policy page offers in its dropdown (§14 #23), so a row for a
    transcription task cannot be pointed at a chat model by hand. Sorted so the UI
    order is stable rather than dependent on dict insertion.
    """
    return sorted(model for model, served in CAPABILITIES.items() if served == capability)


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
