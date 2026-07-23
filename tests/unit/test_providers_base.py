"""Unit tests for the provider abstraction and model→provider routing (§7)."""

import pytest
from pydantic import ValidationError

from llmbus.cost import PRICING
from llmbus.providers.base import (
    CAPABILITIES,
    PROVIDERS,
    Provider,
    ProviderResult,
    UnknownModelError,
    capability_for,
    models_with_capability,
    provider_for,
)
from llmbus.schema import JobParams, Message, Usage

# The routing table spelled out literally, so a mutated value (or a model
# silently re-homed to the wrong provider) is caught.
_EXPECTED_ROUTES = {
    "gpt-5": "openai",
    "gpt-5-mini": "openai",
    "gpt-5-nano": "openai",
    "gpt-5.4-mini": "openai",
    "gpt-5.2": "openai",
    "gpt-5.4": "openai",
    "gpt-5.5": "openai",
    "claude-opus-4-8": "anthropic",
    "claude-sonnet-5": "anthropic",
    "claude-haiku-4-5": "anthropic",
}


# --- Routing table -----------------------------------------------------------


def _assert_routes_match_documented_table(routes):
    assert routes == _EXPECTED_ROUTES


def test_routing_table_matches_documented_routes():
    _assert_routes_match_documented_table(PROVIDERS)


def test_routing_table_canary_rejects_a_rehomed_model():
    wrong_routes = dict(PROVIDERS)
    wrong_routes["gpt-5.4-mini"] = "anthropic"

    with pytest.raises(AssertionError):
        _assert_routes_match_documented_table(wrong_routes)


@pytest.mark.parametrize(("model", "provider"), sorted(_EXPECTED_ROUTES.items()))
def test_provider_for_routes_each_model(model, provider):
    assert provider_for(model) == provider


def test_gpt_5_4_mini_routes_to_openai():
    assert provider_for("gpt-5.4-mini") == "openai"


def test_provider_for_unknown_model_raises():
    with pytest.raises(UnknownModelError, match="gpt-4o-mini"):
        provider_for("gpt-4o-mini")


def test_provider_for_unknown_model_error_is_key_error():
    assert issubclass(UnknownModelError, KeyError)


def test_provider_for_unknown_model_error_names_the_model():
    with pytest.raises(UnknownModelError) as exc_info:
        provider_for("gpt-5-pro")

    assert exc_info.value.args == ("gpt-5-pro",)


def test_provider_for_is_case_sensitive_and_does_not_guess_provider():
    with pytest.raises(UnknownModelError):
        provider_for("GPT-5")
    with pytest.raises(UnknownModelError):
        provider_for("claude")


def test_routing_covers_exactly_the_priced_models():
    # Single source of truth: every priced model must have a route and every
    # routed model must have a price — neither table may drift ahead of the other.
    assert set(PROVIDERS) == set(PRICING)


def test_every_priced_model_has_a_non_empty_string_route():
    for model in PRICING:
        assert isinstance(PROVIDERS[model], str)
        assert PROVIDERS[model]


def test_every_route_names_a_known_provider():
    assert set(PROVIDERS.values()) == {"openai", "anthropic"}


# --- capability (§14 #23: the bus must know what a model is FOR) -------------


def test_capabilities_cover_exactly_the_routed_models():
    # Third table, same lockstep rule as routing vs pricing: a model may never be
    # routed without a capability, nor declared capable without a route. Once the
    # bus picks models centrally, "which provider" is not enough to pick safely.
    assert set(CAPABILITIES) == set(PROVIDERS)


def test_every_capability_is_one_of_the_declared_kinds():
    assert set(CAPABILITIES.values()) <= {"chat", "transcription", "embedding"}


def test_every_model_registered_today_is_a_chat_model():
    # Pins the current state honestly: transcription arrives with §4 v2 (§14 #24).
    # When whisper-1 lands this test changes, and that change is the reminder that
    # the policy UI now has a second capability to filter on.
    assert set(CAPABILITIES.values()) == {"chat"}


def test_capability_for_returns_the_registered_capability():
    assert capability_for("gpt-5-nano") == "chat"


def test_capability_for_is_fail_loud_on_an_unregistered_model():
    # No assumed default: guessing "probably chat" is how a transcription model
    # ends up in a chat call.
    with pytest.raises(UnknownModelError, match="whisper-1"):
        capability_for("whisper-1")


def test_models_with_capability_lists_every_chat_model_sorted():
    assert models_with_capability("chat") == sorted(PROVIDERS)


def test_models_with_capability_is_empty_for_a_capability_nothing_serves_yet():
    # The policy page must render an empty dropdown rather than raise.
    assert models_with_capability("transcription") == []


def test_models_with_capability_excludes_other_capabilities(monkeypatch):
    monkeypatch.setitem(CAPABILITIES, "whisper-1", "transcription")

    assert models_with_capability("transcription") == ["whisper-1"]
    assert "whisper-1" not in models_with_capability("chat")


# --- ProviderResult ----------------------------------------------------------


def test_provider_result_carries_completion_and_usage():
    usage = Usage(input_tokens=10, output_tokens=20)
    result = ProviderResult(completion="hello", usage=usage)
    assert result.completion == "hello"
    assert result.usage is usage


def test_provider_result_defaults_cost_to_zero_pending_pricing():
    # Providers don't price; cost_usd stays 0.0 until cost.py fills it downstream.
    result = ProviderResult(completion="hi", usage=Usage(input_tokens=1, output_tokens=1))
    assert result.usage.cost_usd == 0.0


def test_provider_result_allows_explicit_usage_but_providers_should_not_price():
    result = ProviderResult(completion="hi", usage=Usage(input_tokens=1, output_tokens=1))
    assert result.usage.model_dump(by_alias=True) == {"in": 1, "out": 1, "cost_usd": 0.0}


def test_provider_result_rejects_priced_usage():
    # Providers report tokens only; cost.py is the sole pricing authority (§6),
    # so a non-zero cost_usd reaching a ProviderResult is a contract violation.
    # Assert the full message (not a substring): the developer-facing guidance is
    # part of the contract, and an exact check also closes the mutation gate.
    with pytest.raises(ValueError) as exc_info:
        ProviderResult(
            completion="hi",
            usage=Usage(input_tokens=1, output_tokens=1, cost_usd=0.01),
        )
    assert str(exc_info.value) == (
        "providers must not price a result; leave usage.cost_usd at its "
        "0.0 default (cost.py fills it, §6), got 0.01"
    )


def test_provider_result_usage_cannot_be_repriced_after_construction():
    # The construction guard forbids priced usage; Usage being frozen forbids
    # bypassing it by mutating cost_usd afterward. A shallow-frozen ProviderResult
    # alone would not stop this — the immutability has to live on Usage.
    result = ProviderResult(completion="hi", usage=Usage(input_tokens=1, output_tokens=1))
    with pytest.raises(ValidationError):
        result.usage.cost_usd = 0.02


def test_provider_result_original_usage_reference_cannot_be_repriced_after_construction():
    usage = Usage(input_tokens=1, output_tokens=1)
    result = ProviderResult(completion="hi", usage=usage)

    with pytest.raises(ValidationError):
        usage.cost_usd = 0.02

    assert result.usage.cost_usd == 0.0


@pytest.mark.parametrize("cost_usd", [0, 0.0, -0.0])
def test_provider_result_accepts_exact_zero_cost(cost_usd):
    assert (
        ProviderResult(completion="hi", usage=Usage(cost_usd=cost_usd)).usage.cost_usd == cost_usd
    )


@pytest.mark.parametrize("cost_usd", [0.01, -0.01])
def test_provider_result_rejects_any_non_zero_cost(cost_usd):
    with pytest.raises(ValueError):
        ProviderResult(completion="hi", usage=Usage(cost_usd=cost_usd))


def test_provider_result_is_frozen():
    result = ProviderResult(completion="hi", usage=Usage())
    with pytest.raises(Exception):  # noqa: B017 - FrozenInstanceError is dataclass-internal
        result.completion = "changed"


# --- Provider protocol -------------------------------------------------------


class _ConformingAdapter:
    name = "openai"

    async def call(self, model, messages, params):
        return ProviderResult(completion="ok", usage=Usage())


def test_conforming_adapter_satisfies_provider_protocol():
    assert isinstance(_ConformingAdapter(), Provider)


def test_adapter_missing_call_is_not_a_provider():
    class _NoCall:
        name = "openai"

    assert not isinstance(_NoCall(), Provider)


def test_runtime_protocol_check_is_structural_not_semantic():
    class _SyncCallWrongNameType:
        name = 123

        def call(self, model, messages, params):
            return "not a ProviderResult"

    # runtime_checkable Protocol only checks that attributes exist. Static typing
    # and adapter tests must enforce async call semantics and result shape.
    assert isinstance(_SyncCallWrongNameType(), Provider)


async def test_conforming_adapter_returns_a_provider_result():
    adapter = _ConformingAdapter()
    result = await adapter.call("gpt-5", [Message(role="user", content="hi")], JobParams())
    assert isinstance(result, ProviderResult)
    assert result.completion == "ok"
