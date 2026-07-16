"""Unit tests for the deploy-time VPS smoke check.

The smoke script is intentionally the one place that hits the live prod bus and
the live provider on the VPS. Here we pin only its decision logic by faking our
own BusClient seam: timeout, empty completion, terminal error, and the success
path. No real broker, store, or provider call belongs in a unit test.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

import pytest

from llmbus.schema import Result, Usage

_SMOKE_PATH = Path(__file__).resolve().parents[2] / "deploy" / "smoke.py"
_SPEC = importlib.util.spec_from_file_location("deploy_smoke", _SMOKE_PATH)
assert _SPEC is not None and _SPEC.loader is not None
smoke = importlib.util.module_from_spec(_SPEC)
sys.modules.setdefault("deploy_smoke", smoke)
_SPEC.loader.exec_module(smoke)


def _ok_result(*, completion: str) -> Result:
    return Result(
        job_id="11111111-1111-1111-1111-111111111111",
        status="ok",
        completion=completion,
        provider="openai",
        usage=Usage(input_tokens=11, output_tokens=22, cost_usd=0.000123),
    )


def _error_result() -> Result:
    return Result(
        job_id="11111111-1111-1111-1111-111111111111",
        status="error",
        completion=None,
        provider=None,
        error="provider down",
        usage=Usage(),
    )


class FakeBus:
    def __init__(self, *, result: Result | None = None, timeout: bool = False) -> None:
        self._result = result
        self._timeout = timeout
        self.submitted_job = None
        self.awaited = None

    async def submit(self, job):
        self.submitted_job = job
        return "11111111-1111-1111-1111-111111111111"

    async def await_result(self, job_id: str, *, timeout_s: float):
        self.awaited = {"job_id": job_id, "timeout_s": timeout_s}
        if self._timeout:
            raise TimeoutError("too slow")
        return self._result

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None


def test_smoke_success_returns_zero_and_submits_the_expected_job(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bus = FakeBus(result=_ok_result(completion="pong"))
    monkeypatch.setattr(smoke.BusClient, "from_env", lambda: bus)

    exit_code = asyncio.run(smoke.main())

    assert exit_code == 0
    assert bus.awaited == {
        "job_id": "11111111-1111-1111-1111-111111111111",
        "timeout_s": smoke.TIMEOUT_S,
    }
    assert bus.submitted_job is not None
    assert bus.submitted_job.project == "llmbus-smoke"
    assert bus.submitted_job.kind == "smoke"
    assert bus.submitted_job.model == smoke.MODEL
    assert bus.submitted_job.params.max_tokens == 256
    assert bus.submitted_job.messages[0].content == "Reply with exactly one word: pong"
    out = capsys.readouterr().out
    assert "submitted 11111111-1111-1111-1111-111111111111" in out
    assert "OK: 'pong'" in out


def test_smoke_timeout_returns_non_zero_and_prints_worker_hint(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(smoke.BusClient, "from_env", lambda: FakeBus(timeout=True))

    exit_code = asyncio.run(smoke.main())

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "FAIL: no terminal result after 90s." in out
    assert "systemctl status llmbus-worker" in out


def test_smoke_treats_status_ok_with_empty_completion_as_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(
        smoke.BusClient, "from_env", lambda: FakeBus(result=_ok_result(completion=""))
    )

    exit_code = asyncio.run(smoke.main())

    assert exit_code == 1
    out = capsys.readouterr().out
    assert "FAIL: status=ok but the completion is EMPTY." in out
    assert "22 output tokens were billed and produced no text" in out


def test_smoke_treats_non_ok_terminal_status_as_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(smoke.BusClient, "from_env", lambda: FakeBus(result=_error_result()))

    exit_code = asyncio.run(smoke.main())

    assert exit_code == 1
    assert "FAIL: worker returned status=error: provider down" in capsys.readouterr().out
