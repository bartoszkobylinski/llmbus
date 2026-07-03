"""Unit tests for per-model cost calculation (ARCHITECTURE.md §6/§7)."""

import dataclasses
from decimal import Decimal

import pytest

from llmbus.cost import PRICING, ModelPricing, UnknownModelError, cost_usd, price_for

# --- Pricing table (canary: pins every documented rate) ----------------------


@pytest.mark.parametrize(
    ("model", "input_rate", "output_rate"),
    [
        ("gpt-5", "1.25", "10.00"),
        ("gpt-5-mini", "0.25", "2.00"),
        ("gpt-5-nano", "0.05", "0.40"),
        ("claude-opus-4-8", "5.00", "25.00"),
        ("claude-sonnet-5", "3.00", "15.00"),
        ("claude-haiku-4-5", "1.00", "5.00"),
    ],
)
def test_each_model_priced_at_documented_rates(model, input_rate, output_rate):
    # 1M input (or output) tokens costs exactly the per-MTok rate. This both
    # verifies the numbers and catches input/output rates being swapped.
    assert cost_usd(model, 1_000_000, 0) == Decimal(input_rate)
    assert cost_usd(model, 0, 1_000_000) == Decimal(output_rate)


# --- Calculation -------------------------------------------------------------


def test_cost_is_zero_for_zero_tokens():
    assert cost_usd("gpt-5-mini", 0, 0) == Decimal("0")


def test_cost_sums_input_and_output():
    # gpt-5-mini: $0.25 in / $2.00 out per 1M tokens.
    assert cost_usd("gpt-5-mini", 1_000_000, 1_000_000) == Decimal("2.25")


def test_cost_scales_linearly_with_tokens():
    # 1000 * 0.25/1e6 + 500 * 2.00/1e6 = 0.00025 + 0.001
    assert cost_usd("gpt-5-mini", 1000, 500) == Decimal("0.00125")


def test_cost_returns_decimal_not_float():
    assert isinstance(cost_usd("gpt-5-nano", 10, 10), Decimal)


# --- Failure modes -----------------------------------------------------------


def test_unknown_model_raises():
    with pytest.raises(UnknownModelError):
        cost_usd("gpt-4o-mini", 100, 100)  # deliberately not in the table


def test_unknown_model_error_is_a_key_error():
    assert issubclass(UnknownModelError, KeyError)


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [(-1, 0), (0, -1), (-5, -5)],
)
def test_negative_tokens_raise(input_tokens, output_tokens):
    with pytest.raises(ValueError, match=r"^token counts must be non-negative$"):
        cost_usd("gpt-5-mini", input_tokens, output_tokens)


# --- price_for / ModelPricing ------------------------------------------------


def test_price_for_returns_pricing():
    assert price_for("gpt-5") == ModelPricing(Decimal("1.25"), Decimal("10.00"))


def test_price_for_unknown_raises():
    with pytest.raises(UnknownModelError):
        price_for("nope")


def test_unknown_model_error_names_the_model():
    # The error carries the offending model name, so it isn't a blank KeyError.
    with pytest.raises(UnknownModelError, match="gpt-4o-mini"):
        price_for("gpt-4o-mini")


def test_model_pricing_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        price_for("gpt-5").input_per_mtok = Decimal("0")


def test_pricing_table_covers_both_providers():
    assert any(m.startswith("gpt-") for m in PRICING)
    assert any(m.startswith("claude-") for m in PRICING)
