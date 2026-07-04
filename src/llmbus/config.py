"""Configuration loading and provider wiring (ARCHITECTURE.md §10).

Everything the bus needs that is *policy*, not mechanism, comes from `.env`
(loaded via python-dotenv): provider API keys, per-provider rate limits, and the
Iggy connection. `load_config()` parses that environment into a frozen `Config`;
`build_providers()` turns the keys into the live `dict[str, Provider]` registry
the worker routes through.

Two deliberate seams keep this testable and honest:

- **The environment is injected.** Parsing takes a `Mapping[str, str]`, so the
  pure parse helpers unit-test without touching the real environment or the
  filesystem; only `load_config()`'s default path calls `load_dotenv()` and reads
  `os.environ`.
- **The SDK clients are injected too.** `build_providers()` takes client
  factories, defaulting to the real `AsyncOpenAI`/`AsyncAnthropic` constructors —
  imported *lazily* so merely importing this module never requires the optional
  `worker` extra (`pip install llmbus[worker]`). Producers that only import
  `client.py` never pull in the LLM SDKs.

`build_providers` is the one typed seam where mypy finally verifies the concrete
adapters satisfy the `Provider` protocol (§7, §14 #8): the registry is annotated
`dict[str, Provider]`, so an adapter whose `call`/`name` drifts from the contract
fails the type gate *here*, at the wiring, rather than at runtime.
"""

from __future__ import annotations

import math
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from dotenv import load_dotenv

from llmbus.providers.anthropic import AnthropicAdapter
from llmbus.providers.base import Provider
from llmbus.providers.openai import OpenAIAdapter
from llmbus.ratelimit import ProviderLimits


class ConfigError(ValueError):
    """A required setting is missing or invalid.

    Raised loudly at load time rather than letting the bus boot half-configured
    and fail later on the first job — configuration is startup policy (§10), so a
    bad `.env` should stop the process, not degrade it.
    """


def _require(env: Mapping[str, str], key: str) -> str:
    """Return a non-empty required setting (surrounding whitespace stripped), or
    raise `ConfigError` naming the missing key. A blank/whitespace-only value
    counts as missing — an unfilled `.env` line (`OPENAI_API_KEY=`) must fail."""
    value = env.get(key, "").strip()
    if not value:
        raise ConfigError(f"missing required setting {key}")
    return value


def _positive_float(env: Mapping[str, str], key: str) -> float:
    """Parse a strictly-positive, finite float setting, or raise `ConfigError`.

    Rejects non-numbers, non-positive values, and non-finite ones (`inf`/`nan`) —
    a `nan` would slip past a bare `<= 0` check (all NaN comparisons are false)
    and poison the rate limiter downstream, so it is caught here."""
    raw = _require(env, key)
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"setting {key} must be a number, got {raw!r}") from None
    if not math.isfinite(value) or value <= 0:
        raise ConfigError(f"setting {key} must be a positive finite number, got {value!r}")
    return value


def _provider_limits(env: Mapping[str, str], prefix: str) -> ProviderLimits:
    """Build one provider's limits from `{PREFIX}_RPM` / `{PREFIX}_TPM` (§6)."""
    return ProviderLimits(
        requests_per_min=_positive_float(env, f"{prefix}_RPM"),
        tokens_per_min=_positive_float(env, f"{prefix}_TPM"),
    )


@dataclass(frozen=True)
class Config:
    """Resolved bus configuration (§10). Frozen — settings are fixed after load.

    `frozen=True` only blocks rebinding a field; it does nothing for the *contents*
    of a mutable field. So `rate_limits` is deep-frozen in `__post_init__`: it is
    copied into a read-only `MappingProxyType`, making the "settings are fixed after
    load" contract hold at runtime for every construction path — not just
    `parse_config()` — since `Config` is public and built directly (e.g. tests).
    """

    openai_api_key: str
    anthropic_api_key: str
    rate_limits: Mapping[str, ProviderLimits]
    iggy_address: str
    iggy_username: str
    iggy_password: str

    def __post_init__(self) -> None:
        # Copy first so a caller mutating the dict they passed in can't reach back
        # through the proxy; object.__setattr__ because the dataclass is frozen.
        frozen_limits = MappingProxyType(dict(self.rate_limits))
        object.__setattr__(self, "rate_limits", frozen_limits)


def parse_config(env: Mapping[str, str]) -> Config:
    """Parse a `Config` from an environment mapping.

    Pure — no `.env`, no `os.environ`, no network — so the whole parse surface
    (required keys, per-provider limits, Iggy connection) unit-tests off an
    injected dict. `load_config()` is the thin impure wrapper that supplies the
    real environment.
    """
    return Config(
        openai_api_key=_require(env, "OPENAI_API_KEY"),
        anthropic_api_key=_require(env, "ANTHROPIC_API_KEY"),
        # Passed as a plain dict; Config.__post_init__ deep-freezes it into a
        # read-only mapping, so the immutability guarantee lives in exactly one place.
        rate_limits={
            "openai": _provider_limits(env, "OPENAI"),
            "anthropic": _provider_limits(env, "ANTHROPIC"),
        },
        iggy_address=_require(env, "IGGY_ADDRESS"),
        iggy_username=_require(env, "IGGY_USERNAME"),
        iggy_password=_require(env, "IGGY_PASSWORD"),
    )


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Load config from `.env` + `os.environ`, or from an injected `env` (tests).

    The default path loads `.env` via python-dotenv then parses `os.environ`;
    passing `env` bypasses both, keeping tests off the real environment.
    """
    if env is None:
        load_dotenv()
        env = os.environ
    return parse_config(env)


# A factory that turns an API key into an SDK client. Injected into
# `build_providers` so tests pass fakes and the real SDK import stays lazy.
ClientFactory = Callable[[str], Any]


def _default_openai_client(api_key: str) -> Any:  # pragma: no cover - needs `worker` extra
    from openai import AsyncOpenAI

    return AsyncOpenAI(api_key=api_key)


def _default_anthropic_client(api_key: str) -> Any:  # pragma: no cover - needs `worker` extra
    from anthropic import AsyncAnthropic

    return AsyncAnthropic(api_key=api_key)


def build_providers(
    config: Config,
    *,
    openai_client: ClientFactory = _default_openai_client,
    anthropic_client: ClientFactory = _default_anthropic_client,
) -> dict[str, Provider]:
    """Build the provider registry the worker routes through, keyed by provider
    name (matching `providers.base.PROVIDERS` values).

    The `dict[str, Provider]` annotation is the typed seam (§14 #8): mypy verifies
    `OpenAIAdapter`/`AnthropicAdapter` structurally satisfy `Provider` here. Client
    construction is injected — the defaults build the real async clients (offline;
    they connect on first request, not at construction), while tests pass fakes and
    never trigger the optional SDK import.
    """
    providers: dict[str, Provider] = {
        "openai": OpenAIAdapter(openai_client(config.openai_api_key)),
        "anthropic": AnthropicAdapter(anthropic_client(config.anthropic_api_key)),
    }
    return providers
