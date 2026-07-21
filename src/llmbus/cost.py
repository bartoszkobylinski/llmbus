"""Per-model LLM cost calculation with effective-dated pricing (ARCHITECTURE.md §6/§7).

Given a model, token usage, and the date a job ran, compute its USD cost from a
per-model *price history*. Each model maps to one or more `PricePoint`s (a rate
that takes effect on a date); the rate in force on the job's date is used. So a
scheduled price change — e.g. Claude Sonnet 5's intro rate reverting on
2026-09-01 — resolves automatically, with no manual edit and no clock/network
dependency: the date is passed in (from `job.submitted_at`), never read from the
wall clock or scraped from a provider page.

Money is `Decimal` end-to-end — never float — so the cost ledger doesn't
accumulate rounding error; callers convert to float only at the schema/store
boundary (`Result.usage.cost_usd`).

Rates are USD per 1,000,000 tokens, verified 2026-07-03 against provider pricing
pages (OpenAI: developers.openai.com/api/docs/pricing; Anthropic via the
claude-api reference). When a price changes, append a new `PricePoint` — don't
edit history.
"""

from __future__ import annotations

import dataclasses
from datetime import date
from decimal import Decimal

_PER_MILLION = Decimal(1_000_000)

# Floor date for models we've only ever tracked at a single rate: any realistic
# job date is on/after it, so it always resolves.
_EPOCH = date(2025, 1, 1)


class UnknownModelError(KeyError):
    """No price applies to a (model, date) — raised rather than under-counting cost."""


@dataclasses.dataclass(frozen=True)
class ModelPricing:
    """USD per 1,000,000 tokens, input and output billed separately."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal


@dataclasses.dataclass(frozen=True)
class PricePoint:
    """A rate that takes effect on `effective` (inclusive) until the next point."""

    effective: date
    pricing: ModelPricing


# Per-model price history, oldest first. Verified 2026-07-03.
PRICING: dict[str, tuple[PricePoint, ...]] = {
    # OpenAI — GPT-5 family
    "gpt-5": (PricePoint(_EPOCH, ModelPricing(Decimal("1.25"), Decimal("10.00"))),),
    "gpt-5-mini": (PricePoint(_EPOCH, ModelPricing(Decimal("0.25"), Decimal("2.00"))),),
    "gpt-5-nano": (PricePoint(_EPOCH, ModelPricing(Decimal("0.05"), Decimal("0.40"))),),
    # hate-moderator's classifier model (§14 #6). Rate verified 2026-07-20 against
    # OpenAI's published pricing, and independently equal to the rates hate-mod
    # pins in its own config (`config.py:30-31`) — two sources agreeing, not one
    # copied twice. Evidence: `notes/model-pricing-openai.md`.
    "gpt-5.4-mini": (PricePoint(_EPOCH, ModelPricing(Decimal("0.75"), Decimal("4.50"))),),
    # Anthropic
    "claude-opus-4-8": (PricePoint(_EPOCH, ModelPricing(Decimal("5.00"), Decimal("25.00"))),),
    "claude-haiku-4-5": (PricePoint(_EPOCH, ModelPricing(Decimal("1.00"), Decimal("5.00"))),),
    # Sonnet 5: intro rate through 2026-08-31, standard rate from 2026-09-01.
    "claude-sonnet-5": (
        PricePoint(_EPOCH, ModelPricing(Decimal("2.00"), Decimal("10.00"))),
        PricePoint(date(2026, 9, 1), ModelPricing(Decimal("3.00"), Decimal("15.00"))),
    ),
}


def price_for(model: str, on: date) -> ModelPricing:
    """The rate in effect for `model` on date `on`, or raise `UnknownModelError`."""
    points = PRICING.get(model)
    if points is None:
        raise UnknownModelError(model)
    applicable = [point for point in points if point.effective <= on]
    if not applicable:
        raise UnknownModelError(f"{model!r} has no price effective on {on.isoformat()}")
    return max(applicable, key=lambda point: point.effective).pricing


def cost_usd(model: str, input_tokens: int, output_tokens: int, on: date) -> Decimal:
    """USD cost of a completion, priced at the rate in effect on date `on`.

    Raises `UnknownModelError` when no price applies and `ValueError` for negative
    token counts — both are bugs we want loud, not a silently wrong ledger.
    """
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("token counts must be non-negative")
    pricing = price_for(model, on)
    return (
        input_tokens * pricing.input_per_mtok + output_tokens * pricing.output_per_mtok
    ) / _PER_MILLION
