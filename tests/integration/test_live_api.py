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

from llmbus.config import Config, build_providers  # noqa: E402
from llmbus.schema import JobParams, Message  # noqa: E402

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
