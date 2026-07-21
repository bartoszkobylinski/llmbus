"""Unit tests for effective-dated per-model cost calculation (ARCHITECTURE.md §6/§7)."""

import dataclasses
from datetime import date
from decimal import Decimal
from types import MappingProxyType

import pytest

from llmbus.cost import PRICING, ModelPricing, PricePoint, UnknownModelError, cost_usd, price_for

_TODAY = date(2026, 7, 3)  # a date where every model has a single unambiguous rate
_INTRO_LAST_DAY = date(2026, 8, 31)  # last day of Sonnet 5's intro rate
_STANDARD_FIRST_DAY = date(2026, 9, 1)  # Sonnet 5 reverts to standard here

# The full table, spelled out with literal dates so a mutated rate OR effective
# date is caught (a canary that also documents the pricing history).
_DOCUMENTED_PRICING = MappingProxyType(
    {
        "gpt-5": (PricePoint(date(2025, 1, 1), ModelPricing(Decimal("1.25"), Decimal("10.00"))),),
        "gpt-5-mini": (
            PricePoint(date(2025, 1, 1), ModelPricing(Decimal("0.25"), Decimal("2.00"))),
        ),
        "gpt-5-nano": (
            PricePoint(date(2025, 1, 1), ModelPricing(Decimal("0.05"), Decimal("0.40"))),
        ),
        "gpt-5.4-mini": (
            PricePoint(date(2025, 1, 1), ModelPricing(Decimal("0.75"), Decimal("4.50"))),
        ),
        "claude-opus-4-8": (
            PricePoint(date(2025, 1, 1), ModelPricing(Decimal("5.00"), Decimal("25.00"))),
        ),
        "claude-haiku-4-5": (
            PricePoint(date(2025, 1, 1), ModelPricing(Decimal("1.00"), Decimal("5.00"))),
        ),
        "claude-sonnet-5": (
            PricePoint(date(2025, 1, 1), ModelPricing(Decimal("2.00"), Decimal("10.00"))),
            PricePoint(date(2026, 9, 1), ModelPricing(Decimal("3.00"), Decimal("15.00"))),
        ),
    }
)


# --- Pricing table (canary) --------------------------------------------------


def test_pricing_table_matches_documented_history():
    assert PRICING == _DOCUMENTED_PRICING


@pytest.mark.parametrize(
    ("model", "on", "input_rate", "output_rate"),
    [
        ("gpt-5", _TODAY, "1.25", "10.00"),
        ("gpt-5-mini", _TODAY, "0.25", "2.00"),
        ("gpt-5-nano", _TODAY, "0.05", "0.40"),
        ("claude-opus-4-8", _TODAY, "5.00", "25.00"),
        ("claude-haiku-4-5", _TODAY, "1.00", "5.00"),
        # Sonnet 5 is date-dependent:
        ("claude-sonnet-5", _INTRO_LAST_DAY, "2.00", "10.00"),
        ("claude-sonnet-5", _STANDARD_FIRST_DAY, "3.00", "15.00"),
    ],
)
def test_each_model_priced_at_documented_rates(model, on, input_rate, output_rate):
    # 1M input (or output) tokens costs exactly the per-MTok rate — verifies the
    # numbers and catches input/output rates being swapped.
    assert cost_usd(model, 1_000_000, 0, on) == Decimal(input_rate)
    assert cost_usd(model, 0, 1_000_000, on) == Decimal(output_rate)


def test_pricing_rates_are_decimal_values_not_float():
    for points in PRICING.values():
        for point in points:
            assert isinstance(point.pricing.input_per_mtok, Decimal)
            assert isinstance(point.pricing.output_per_mtok, Decimal)


def test_pricing_histories_follow_oldest_first_convention():
    for points in PRICING.values():
        effective_dates = [point.effective for point in points]
        assert effective_dates == sorted(effective_dates)


# --- Effective-dated resolution ----------------------------------------------


@pytest.mark.parametrize(
    ("on", "expected_input_rate"),
    [
        (date(2026, 8, 30), "2.00"),  # before cutoff → intro
        (_INTRO_LAST_DAY, "2.00"),  # last intro day (boundary: cutoff is exclusive of the old rate)
        (_STANDARD_FIRST_DAY, "3.00"),  # cutoff day → standard (boundary: effective is inclusive)
        (date(2026, 9, 2), "3.00"),  # after cutoff → standard
    ],
)
def test_sonnet5_rate_switches_on_the_cutoff_date(on, expected_input_rate):
    assert cost_usd("claude-sonnet-5", 1_000_000, 0, on) == Decimal(expected_input_rate)


def test_sonnet5_output_rate_switches_on_the_cutoff_date():
    assert cost_usd("claude-sonnet-5", 0, 1_000_000, _INTRO_LAST_DAY) == Decimal("10.00")
    assert cost_usd("claude-sonnet-5", 0, 1_000_000, _STANDARD_FIRST_DAY) == Decimal("15.00")


def test_cost_uses_supplied_job_date_not_current_date():
    # This test should pass regardless of the calendar date on the machine
    # running it: the caller supplies the job's submitted_at date.
    assert cost_usd("claude-sonnet-5", 1_000_000, 1_000_000, _INTRO_LAST_DAY) == Decimal("12.00")
    assert cost_usd("claude-sonnet-5", 1_000_000, 1_000_000, _STANDARD_FIRST_DAY) == Decimal(
        "18.00"
    )


def test_price_for_chooses_latest_effective_point_before_or_on_date(monkeypatch):
    monkeypatch.setitem(
        PRICING,
        "test-multi-point",
        (
            PricePoint(date(2030, 1, 1), ModelPricing(Decimal("30.00"), Decimal("300.00"))),
            PricePoint(date(2020, 1, 1), ModelPricing(Decimal("10.00"), Decimal("100.00"))),
            PricePoint(date(2025, 1, 1), ModelPricing(Decimal("20.00"), Decimal("200.00"))),
        ),
    )

    assert price_for("test-multi-point", date(2024, 12, 31)) == ModelPricing(
        Decimal("10.00"), Decimal("100.00")
    )
    assert price_for("test-multi-point", date(2025, 1, 1)) == ModelPricing(
        Decimal("20.00"), Decimal("200.00")
    )
    assert price_for("test-multi-point", date(2029, 12, 31)) == ModelPricing(
        Decimal("20.00"), Decimal("200.00")
    )
    assert price_for("test-multi-point", date(2030, 1, 1)) == ModelPricing(
        Decimal("30.00"), Decimal("300.00")
    )


def test_no_price_before_earliest_effective_date():
    with pytest.raises(UnknownModelError, match="has no price effective on"):
        cost_usd("gpt-5", 1, 1, date(2024, 12, 31))  # before the 2025-01-01 floor


# --- Calculation -------------------------------------------------------------


def test_cost_is_zero_for_zero_tokens():
    assert cost_usd("gpt-5-mini", 0, 0, _TODAY) == Decimal("0")


def test_cost_sums_input_and_output():
    # gpt-5-mini: $0.25 in / $2.00 out per 1M tokens.
    assert cost_usd("gpt-5-mini", 1_000_000, 1_000_000, _TODAY) == Decimal("2.25")


def test_cost_scales_linearly_with_tokens():
    # 1000 * 0.25/1e6 + 500 * 2.00/1e6 = 0.00025 + 0.001
    assert cost_usd("gpt-5-mini", 1000, 500, _TODAY) == Decimal("0.00125")


def test_cost_asymmetric_mixed_tokens():
    # gpt-5: 123456 * 1.25/1e6 + 7890 * 10.00/1e6 = 0.15432 + 0.0789
    assert cost_usd("gpt-5", 123_456, 7_890, _TODAY) == Decimal("0.23322")


def test_cost_keeps_sub_cent_precision_without_float_rounding():
    # A float implementation would not represent this exact decimal amount.
    assert cost_usd("gpt-5", 1, 1, _TODAY) == Decimal("0.00001125")


def test_cost_handles_large_token_counts_exactly():
    assert cost_usd("gpt-5", 999_999_999_999_999_999, 888_888_888_888_888_888, _TODAY) == Decimal(
        "10138888888888.88887875"
    )


def test_cost_returns_decimal_not_float():
    assert isinstance(cost_usd("gpt-5-nano", 10, 10, _TODAY), Decimal)


# --- Failure modes -----------------------------------------------------------


def test_unknown_model_raises():
    with pytest.raises(UnknownModelError):
        cost_usd("unpriced-model-xyz", 100, 100, _TODAY)  # deliberately not in the table


def test_unknown_model_error_is_a_key_error():
    assert issubclass(UnknownModelError, KeyError)


def test_unknown_model_error_names_the_model():
    # The error carries the offending model name, so it isn't a blank KeyError.
    with pytest.raises(UnknownModelError, match="unpriced-model-xyz"):
        price_for("unpriced-model-xyz", _TODAY)


@pytest.mark.parametrize(
    ("input_tokens", "output_tokens"),
    [(-1, 0), (0, -1), (-5, -5)],
)
def test_negative_tokens_raise(input_tokens, output_tokens):
    with pytest.raises(ValueError, match=r"^token counts must be non-negative$"):
        cost_usd("gpt-5-mini", input_tokens, output_tokens, _TODAY)


def test_unknown_model_and_negative_token_errors_are_distinct():
    with pytest.raises(UnknownModelError):
        cost_usd("unpriced-model-xyz", 0, 0, _TODAY)
    with pytest.raises(ValueError):
        cost_usd("gpt-5-mini", -1, 0, _TODAY)


# --- price_for / value objects -----------------------------------------------


def test_price_for_returns_pricing_in_effect():
    assert price_for("gpt-5", _TODAY) == ModelPricing(Decimal("1.25"), Decimal("10.00"))


def test_price_for_unknown_raises():
    with pytest.raises(UnknownModelError):
        price_for("nope", _TODAY)


def test_model_pricing_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        price_for("gpt-5", _TODAY).input_per_mtok = Decimal("0")


def test_price_point_is_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        PRICING["gpt-5"][0].effective = date(2000, 1, 1)


def test_pricing_table_covers_both_providers():
    assert any(m.startswith("gpt-") for m in PRICING)
    assert any(m.startswith("claude-") for m in PRICING)
