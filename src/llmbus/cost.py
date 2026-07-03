"""Per-model LLM cost calculation (ARCHITECTURE.md §6/§7).

Given a model name and token usage, compute the USD cost from a static pricing
table. Money is `Decimal` end-to-end — never float — so the cost ledger doesn't
accumulate rounding error; callers convert to float only at the schema/store
boundary (`Result.usage.cost_usd`).

Prices are USD per 1,000,000 tokens, verified 2026-07-03 against provider pricing
pages (OpenAI: developers.openai.com/api/docs/pricing; Anthropic via the
claude-api reference). Refresh periodically — provider prices change (e.g. Claude
Sonnet 5 carries an intro discount through 2026-08-31; the standard rate is used
here).
"""

from __future__ import annotations

import dataclasses
from decimal import Decimal

_PER_MILLION = Decimal(1_000_000)


class UnknownModelError(KeyError):
    """No pricing entry for a model — raised rather than silently under-counting."""


@dataclasses.dataclass(frozen=True)
class ModelPricing:
    """USD per 1,000,000 tokens, input and output billed separately."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal


# Verified 2026-07-03. Extend as models are pinned (ARCHITECTURE.md §14 #6).
PRICING: dict[str, ModelPricing] = {
    # OpenAI — GPT-5 family
    "gpt-5": ModelPricing(Decimal("1.25"), Decimal("10.00")),
    "gpt-5-mini": ModelPricing(Decimal("0.25"), Decimal("2.00")),
    "gpt-5-nano": ModelPricing(Decimal("0.05"), Decimal("0.40")),
    # Anthropic — standard rates (Sonnet 5 has an intro discount to 2026-08-31)
    "claude-opus-4-8": ModelPricing(Decimal("5.00"), Decimal("25.00")),
    "claude-sonnet-5": ModelPricing(Decimal("3.00"), Decimal("15.00")),
    "claude-haiku-4-5": ModelPricing(Decimal("1.00"), Decimal("5.00")),
}


def price_for(model: str) -> ModelPricing:
    """Look up a model's pricing, or raise `UnknownModelError`."""
    try:
        return PRICING[model]
    except KeyError as exc:
        raise UnknownModelError(model) from exc


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> Decimal:
    """USD cost of a completion, from the per-model pricing table.

    Raises `UnknownModelError` for an unpriced model and `ValueError` for negative
    token counts — both are bugs we want loud, not a silently wrong ledger.
    """
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    pricing = price_for(model)
    return (
        input_tokens * pricing.input_per_mtok + output_tokens * pricing.output_per_mtok
    ) / _PER_MILLION
