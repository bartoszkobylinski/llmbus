"""Unit tests for config loading and provider wiring (§10).

The environment is injected as a plain dict and the SDK-client factories are
faked, so nothing here touches the real `.env`, `os.environ`, the network, or the
optional `openai`/`anthropic` packages.
"""

import pytest

from llmbus import config
from llmbus.config import (
    Config,
    ConfigError,
    _positive_float,
    _provider_limits,
    _require,
    build_providers,
    iggy_connection_string,
    load_config,
    parse_config,
)
from llmbus.providers.anthropic import AnthropicAdapter
from llmbus.providers.base import PROVIDERS, Provider
from llmbus.providers.openai import OpenAIAdapter
from llmbus.ratelimit import ProviderLimits

# A complete, valid environment. Individual tests drop/override keys from it.
_ENV = {
    "OPENAI_API_KEY": "sk-openai",
    "ANTHROPIC_API_KEY": "sk-anthropic",
    "OPENAI_RPM": "500",
    "OPENAI_TPM": "200000",
    "ANTHROPIC_RPM": "50",
    "ANTHROPIC_TPM": "40000",
    "IGGY_ADDRESS": "127.0.0.1:8090",
    "IGGY_USERNAME": "iggy",
    "IGGY_PASSWORD": "iggy",
    "STORE_PATH": "llmbus.db",
}


def _env(**overrides):
    """A copy of the valid env with keys overridden; a value of None drops the key."""
    env = dict(_ENV)
    for key, value in overrides.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = value
    return env


# --- _require ----------------------------------------------------------------


def test_require_returns_present_value():
    assert _require({"K": "v"}, "K") == "v"


def test_require_strips_surrounding_whitespace():
    assert _require({"K": "  v  "}, "K") == "v"


def test_require_raises_naming_the_missing_key():
    with pytest.raises(ConfigError) as exc_info:
        _require({}, "OPENAI_API_KEY")
    assert str(exc_info.value) == "missing required setting OPENAI_API_KEY"


@pytest.mark.parametrize("blank", ["", "   ", "\t"])
def test_require_treats_blank_as_missing(blank):
    with pytest.raises(ConfigError, match="missing required setting K"):
        _require({"K": blank}, "K")


# --- _positive_float ---------------------------------------------------------


def test_positive_float_parses_a_number():
    assert _positive_float({"K": "500"}, "K") == 500.0


def test_positive_float_rejects_non_number():
    with pytest.raises(ConfigError) as exc_info:
        _positive_float({"K": "fast"}, "K")
    assert str(exc_info.value) == "setting K must be a number, got 'fast'"


@pytest.mark.parametrize("bad", ["0", "-1", "-0.5"])
def test_positive_float_rejects_non_positive(bad):
    with pytest.raises(ConfigError) as exc_info:
        _positive_float({"K": bad}, "K")
    assert str(exc_info.value) == f"setting K must be a positive finite number, got {float(bad)!r}"


@pytest.mark.parametrize("bad", ["inf", "-inf", "nan"])
def test_positive_float_rejects_non_finite(bad):
    # `nan` in particular would slip past a bare `<= 0` guard (all NaN comparisons
    # are false), so the finite check must reject it explicitly.
    with pytest.raises(ConfigError, match="must be a positive finite number"):
        _positive_float({"K": bad}, "K")


def test_positive_float_accepts_small_positive_value():
    assert _positive_float({"K": "0.001"}, "K") == 0.001


def test_positive_float_reuses_require_so_missing_is_reported():
    with pytest.raises(ConfigError, match="missing required setting OPENAI_RPM"):
        _positive_float({}, "OPENAI_RPM")


# --- _provider_limits --------------------------------------------------------


def test_provider_limits_reads_prefixed_rpm_and_tpm():
    limits = _provider_limits(_ENV, "OPENAI")
    assert limits == ProviderLimits(requests_per_min=500.0, tokens_per_min=200000.0)


def test_provider_limits_reports_the_missing_prefixed_key():
    with pytest.raises(ConfigError, match="missing required setting ANTHROPIC_TPM"):
        _provider_limits(_env(ANTHROPIC_TPM=None), "ANTHROPIC")


# --- parse_config ------------------------------------------------------------


def test_parse_config_reads_every_field():
    cfg = parse_config(_ENV)
    assert cfg == Config(
        openai_api_key="sk-openai",
        anthropic_api_key="sk-anthropic",
        rate_limits={
            "openai": ProviderLimits(requests_per_min=500.0, tokens_per_min=200000.0),
            "anthropic": ProviderLimits(requests_per_min=50.0, tokens_per_min=40000.0),
        },
        iggy_address="127.0.0.1:8090",
        iggy_username="iggy",
        iggy_password="iggy",
        db_path="llmbus.db",
    )


def test_parse_config_is_frozen():
    cfg = parse_config(_ENV)
    with pytest.raises(AttributeError):
        cfg.openai_api_key = "sk-other"  # type: ignore[misc]


def test_parse_config_rate_limits_are_immutable():
    cfg = parse_config(_ENV)
    with pytest.raises(TypeError):
        cfg.rate_limits["openai"] = ProviderLimits(requests_per_min=1, tokens_per_min=1)


def test_config_constructor_rate_limits_are_immutable():
    cfg = Config(
        openai_api_key="sk-openai",
        anthropic_api_key="sk-anthropic",
        rate_limits={
            "openai": ProviderLimits(requests_per_min=500, tokens_per_min=200000),
        },
        iggy_address="127.0.0.1:8090",
        iggy_username="iggy",
        iggy_password="iggy",
        db_path="llmbus.db",
    )

    with pytest.raises(TypeError):
        cfg.rate_limits["openai"] = ProviderLimits(requests_per_min=1, tokens_per_min=1)


def test_config_constructor_copies_rate_limits_before_freezing():
    original_limits = {
        "openai": ProviderLimits(requests_per_min=500, tokens_per_min=200000),
    }
    cfg = Config(
        openai_api_key="sk-openai",
        anthropic_api_key="sk-anthropic",
        rate_limits=original_limits,
        iggy_address="127.0.0.1:8090",
        iggy_username="iggy",
        iggy_password="iggy",
        db_path="llmbus.db",
    )

    original_limits["openai"] = ProviderLimits(requests_per_min=1, tokens_per_min=1)

    assert cfg.rate_limits["openai"] == ProviderLimits(
        requests_per_min=500,
        tokens_per_min=200000,
    )


@pytest.mark.parametrize(
    "missing",
    [
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "OPENAI_RPM",
        "OPENAI_TPM",
        "ANTHROPIC_RPM",
        "ANTHROPIC_TPM",
        "IGGY_ADDRESS",
        "IGGY_USERNAME",
        "IGGY_PASSWORD",
        "STORE_PATH",
    ],
)
def test_parse_config_requires_every_setting(missing):
    with pytest.raises(ConfigError, match=f"missing required setting {missing}"):
        parse_config(_env(**{missing: None}))


def test_parse_config_rate_limits_are_keyed_by_provider_name():
    # Registry wiring routes by provider name, so limits must key the same way.
    assert set(parse_config(_ENV).rate_limits) == set(PROVIDERS.values())


# --- load_config -------------------------------------------------------------


def test_load_config_parses_injected_env_without_touching_dotenv(monkeypatch):
    # An injected env must bypass load_dotenv entirely; blow up if it is called.
    monkeypatch.setattr(config, "load_dotenv", _fail_if_called)
    assert load_config(_ENV) == parse_config(_ENV)


def test_load_config_reads_dotenv_then_environ_by_default(monkeypatch):
    # Cover the default path deterministically: stub load_dotenv (don't read the
    # real .env) and drive os.environ. parse_config only reads the keys it needs,
    # so other process env vars are harmless.
    loaded = {"called": False}

    def _fake_load_dotenv():
        loaded["called"] = True

    monkeypatch.setattr(config, "load_dotenv", _fake_load_dotenv)
    for key, value in _ENV.items():
        monkeypatch.setenv(key, value)

    cfg = load_config()

    assert loaded["called"] is True
    assert cfg == parse_config(_ENV)


def _fail_if_called():
    raise AssertionError("load_dotenv must not run when env is injected")


# --- build_providers ---------------------------------------------------------


def _fake_openai_factory(recorder):
    def factory(api_key):
        recorder["openai_key"] = api_key
        return f"openai-client<{api_key}>"

    return factory


def _fake_anthropic_factory(recorder):
    def factory(api_key):
        recorder["anthropic_key"] = api_key
        return f"anthropic-client<{api_key}>"

    return factory


def _providers_with_fakes(recorder=None):
    recorder = {} if recorder is None else recorder
    return build_providers(
        parse_config(_ENV),
        openai_client=_fake_openai_factory(recorder),
        anthropic_client=_fake_anthropic_factory(recorder),
    )


def test_build_providers_passes_each_api_key_to_its_factory():
    recorder = {}
    _providers_with_fakes(recorder)
    assert recorder == {"openai_key": "sk-openai", "anthropic_key": "sk-anthropic"}


def test_build_providers_with_injected_factories_does_not_import_sdks(monkeypatch):
    def _reject_sdk_import(name, *args, **kwargs):
        if name in {"openai", "anthropic"}:
            raise AssertionError(f"{name} must not be imported when factories are injected")
        return original_import(name, *args, **kwargs)

    original_import = __import__
    monkeypatch.setattr("builtins.__import__", _reject_sdk_import)

    _providers_with_fakes()


def test_build_providers_wires_the_injected_client_into_each_adapter():
    providers = _providers_with_fakes()
    assert isinstance(providers["openai"], OpenAIAdapter)
    assert isinstance(providers["anthropic"], AnthropicAdapter)
    assert providers["openai"]._client == "openai-client<sk-openai>"
    assert providers["anthropic"]._client == "anthropic-client<sk-anthropic>"


def test_build_providers_keys_match_adapter_names():
    for name, provider in _providers_with_fakes().items():
        assert provider.name == name


def test_build_providers_registry_covers_every_routed_provider():
    # Every provider name PROVIDERS routes a model to must have an adapter, or a
    # valid model would hit a missing route at call time.
    assert set(_providers_with_fakes()) == set(PROVIDERS.values())


def test_build_providers_adapters_satisfy_provider_protocol():
    for provider in _providers_with_fakes().values():
        assert isinstance(provider, Provider)


# --- iggy_connection_string (§14 #16) ----------------------------------------
#
# Why this function exists at all: IggyClient(address) leaves the SDK's auto_login
# Disabled, so connect() does not authenticate and the SDK's internal reconnect
# (send_raw_with_response -> disconnect -> connect -> retry) silently comes back on an
# UNAUTHENTICATED session. The connection-string form sets auto_login Enabled, so the
# SDK re-authenticates on every reconnect. Verified against the live broker.


def _conn(config):
    return iggy_connection_string(config.iggy_address, config.iggy_username, config.iggy_password)


def _cfg(**overrides):
    data = {
        "openai_api_key": "sk-o",
        "anthropic_api_key": "sk-a",
        "rate_limits": {},
        "iggy_address": "127.0.0.1:8092",
        "iggy_username": "iggy",
        "iggy_password": "secret",
        "db_path": "llmbus.db",
    }
    data.update(overrides)
    return Config(**data)


def test_connection_string_carries_credentials_and_address():
    assert _conn(_cfg()) == "iggy+tcp://iggy:secret@127.0.0.1:8092"


def test_connection_string_uses_the_tcp_protocol_scheme():
    # The SDK rejects a connection string whose protocol is not tcp (from_connection_string
    # -> parse_protocol != Tcp -> InvalidConnectionString), and our broker is TCP-only (§9b).
    assert _conn(_cfg()).startswith("iggy+tcp://")


@pytest.mark.parametrize(
    ("password", "encoded"),
    [
        ("p@ss", "p%40ss"),  # @ would otherwise start the host part
        ("p:ss", "p%3Ass"),  # : would otherwise split user/password
        ("p/ss", "p%2Fss"),  # / would otherwise start the path
        ("p ss", "p%20ss"),
        ("pąss", "p%C4%85ss"),  # non-ascii must not land raw in a URL
    ],
)
def test_connection_string_percent_encodes_the_password(password, encoded):
    # Credentials are user-supplied .env values. An unescaped @/:// would reshape the
    # URL and the SDK would parse a different host or user entirely — silently
    # connecting somewhere else or failing with an opaque error.
    assert _conn(_cfg(iggy_password=password)) == (f"iggy+tcp://iggy:{encoded}@127.0.0.1:8092")


def test_connection_string_percent_encodes_the_username():
    assert _conn(_cfg(iggy_username="a@b")) == ("iggy+tcp://a%40b:secret@127.0.0.1:8092")
