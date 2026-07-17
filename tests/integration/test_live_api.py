"""Live-API smoke test (marker: `live_api`) — NOT part of the automated gate.

This makes a REAL, billable Anthropic call to confirm the one provider fact the
unit tests can only assume: that `claude-haiku-4-5` **accepts** a caller-set
`temperature` (inferred from Anthropic's docs, not yet confirmed live), unlike
opus/sonnet and the GPT-5 family, which reject one. `anthropic.py`'s
`_MODELS_SUPPORTING_TEMPERATURE` encodes that inference — a wrong guess would only
surface here, as a 400 from a real call.

It exercises the real wiring end-to-end: `config.build_providers` constructs the
actual `AsyncAnthropic` client and the `AnthropicAdapter` calls it.

Skips unless both the `worker` extra (openai + anthropic SDKs) is installed AND
`ANTHROPIC_API_KEY` is set, so it never runs — or fails — in the default suite.
Run it deliberately, with real keys, to confirm the inference:

    uv run pytest -m live_api
"""

import os

import pytest

pytest.importorskip("anthropic", reason="live_api needs the `worker` extra (anthropic SDK)")
pytest.importorskip("openai", reason="live_api needs the `worker` extra (openai SDK)")

import json  # noqa: E402

from llmbus.config import Config, build_providers  # noqa: E402
from llmbus.schema import JobParams, Message, ResponseFormat  # noqa: E402

pytestmark = [
    pytest.mark.live_api,
    pytest.mark.skipif(
        not os.environ.get("ANTHROPIC_API_KEY"),
        reason="ANTHROPIC_API_KEY not set — live_api tests make real billable calls",
    ),
]


def _live_config() -> Config:
    # A minimal Config built straight from the real key — enough to wire the
    # provider registry without requiring the full rate-limit/Iggy env. The
    # OpenAI key is unused here but its client still constructs offline.
    return Config(
        openai_api_key=os.environ.get("OPENAI_API_KEY", "unused"),
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        rate_limits={},
        iggy_address="",
        iggy_username="",
        iggy_password="",
        db_path=":memory:",
    )


async def test_haiku_accepts_caller_set_temperature():
    provider = build_providers(_live_config())["anthropic"]

    result = await provider.call(
        "claude-haiku-4-5",
        [Message(role="user", content="Reply with the single word: ok")],
        JobParams(max_tokens=16, temperature=0.5),
    )

    # No 400 means Haiku honored the temperature — the inference holds.
    assert isinstance(result.completion, str)
    assert result.usage.output_tokens > 0


# --- structured output (§14 #10) ---------------------------------------------
# Type introspection against the pinned SDKs proved the request SHAPE compiles;
# only a real call proves the SERVER accepts our exact mapping. One test per
# provider: the completion must parse as JSON matching the schema — that is the
# whole guarantee the field exists to provide.

_VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "category": {"type": "string", "enum": ["neutral", "hate"]},
        "confidence": {"type": "number"},
    },
    "required": ["category", "confidence"],
    "additionalProperties": False,
}

# GPT-5's `max_completion_tokens` budget covers reasoning tokens too: measured live
# (2026-07-17), gpt-5-nano spent 448 reasoning tokens on this one-line prompt, so a
# 128 budget ends with finish_reason "length" and an EMPTY completion. 2048 leaves
# headroom; billing is per actual usage, so the headroom costs nothing.
_VERDICT_PARAMS = JobParams(
    max_tokens=2048,
    response_format=ResponseFormat(name="verdict", json_schema=_VERDICT_SCHEMA),
)

_VERDICT_PROMPT = [
    Message(
        role="user",
        content='Classify the comment "have a nice day" as neutral or hate.',
    )
]


def _assert_verdict(completion: str) -> None:
    verdict = json.loads(completion)
    assert set(verdict) == {"category", "confidence"}
    assert verdict["category"] in ("neutral", "hate")
    assert isinstance(verdict["confidence"], (int, float))


async def test_anthropic_accepts_output_config_json_schema():
    provider = build_providers(_live_config())["anthropic"]

    result = await provider.call("claude-haiku-4-5", _VERDICT_PROMPT, _VERDICT_PARAMS)

    _assert_verdict(result.completion)


@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — this live_api test makes a real billable OpenAI call",
)
async def test_gpt5_accepts_strict_json_schema_response_format():
    provider = build_providers(_live_config())["openai"]

    result = await provider.call("gpt-5-nano", _VERDICT_PROMPT, _VERDICT_PARAMS)

    _assert_verdict(result.completion)
