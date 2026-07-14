"""End-to-end smoke test against the LIVE bus. Run on the VPS, after a deploy.

    cd ~/Projects/llmbus && .venv/bin/python deploy/smoke.py

Proves the whole path, which "the worker is idle" does not: submit -> Iggy
(`llmbus`/`llm-jobs`) -> worker consumes -> provider call -> Result in the store ->
producer polls it back with `await_result` (§3, §14 #7). Exits non-zero on any failure,
so it can gate a deploy.

It makes ONE real, paid model call (gpt-5-nano — a fraction of a cent) and leaves ONE
real message on the prod topic. That is deliberate: a smoke test that mocks the provider
or uses a throwaway stream proves nothing about prod. It does NOT create or delete
streams (unlike tests/integration/, which must never be aimed at this broker).

`max_tokens` is 256, not the ~16 the prompt needs: on the GPT-5 family, reasoning tokens
are drawn from the SAME budget, so a small cap is spent entirely on reasoning and the
call returns `status="ok"` with an EMPTY completion. (Observed on the first smoke run:
16/16 output tokens, completion `''`.) Hence the assert below — an empty completion is a
FAILURE here, however "ok" the status looks.
"""

from __future__ import annotations

import asyncio
import sys

from llmbus.client import BusClient
from llmbus.schema import Job, JobParams, Message

MODEL = "gpt-5-nano"
TIMEOUT_S = 90.0


async def main() -> int:
    job = Job(
        project="llmbus-smoke",
        kind="smoke",
        model=MODEL,
        messages=[Message(role="user", content="Reply with exactly one word: pong")],
        params=JobParams(max_tokens=256),
    )

    async with BusClient.from_env() as bus:
        job_id = await bus.submit(job)
        print(f"submitted {job_id} ({MODEL}) — waiting up to {TIMEOUT_S:.0f}s …")

        try:
            result = await bus.await_result(job_id, timeout_s=TIMEOUT_S)
        except TimeoutError:
            print(f"FAIL: no terminal result after {TIMEOUT_S:.0f}s.")
            print("      The job is on the topic but nothing finalized it — is the")
            print("      worker up?  systemctl status llmbus-worker")
            return 1

    if result.status != "ok":
        print(f"FAIL: worker returned status={result.status}: {result.error}")
        return 1

    usage = result.usage
    if not (result.completion or "").strip():
        print("FAIL: status=ok but the completion is EMPTY.")
        print(
            f"      {usage.output_tokens} output tokens were billed and produced no text — "
            "on GPT-5 models that means max_tokens was spent entirely on reasoning."
        )
        return 1

    print(f"OK: {result.completion!r}")
    print(
        f"    provider={result.provider} "
        f"tokens in/out={usage.input_tokens}/{usage.output_tokens} "
        f"cost=${usage.cost_usd:.6f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
