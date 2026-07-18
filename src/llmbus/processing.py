"""The worker's job-processing core (ARCHITECTURE.md §6).

`process_job` is everything the worker does with ONE job once it has been pulled
off Iggy: reserve rate-limit budget, call the provider with retry/backoff and a
per-attempt timeout, price the usage, write the terminal `Result` to the store
(one-shot, §6 idempotency), and POST the callback. It is deliberately free of any
Iggy or network import — the store, providers, rate limiter, callback sender,
sleep, RNG, and timeout runner are all injected via `WorkerDeps` — so the
reliability-critical path (retry, idempotency, cost) is unit- and
mutation-tested with fakes, no live server. The thin Iggy consumer shell that
feeds jobs in lives in `worker.py` (the loop PR).

Idempotency (§6): `submit()` inserts a `pending` row; `process_job` flips it to a
terminal `Result` via the store's one-shot `finalize` (`WHERE status='pending'`).
A redelivered job (at-least-once, e.g. a crash before the Iggy offset committed)
re-runs the model call but `finalize` returns False, so no duplicate row and no
duplicate callback. Callbacks are therefore **best-effort**: the reliable path is
the store (poll, §3/§11); a crash between finalize and callback can drop the
callback, and we do not retry it in v1 (§13).
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import hmac
import logging
import random
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from llmbus.cost import cost_usd
from llmbus.providers.base import Provider, ProviderResult, UnknownModelError, provider_for
from llmbus.ratelimit import RateLimiter
from llmbus.retry import WorkerPolicy, backoff_delay, is_retryable
from llmbus.schema import Job, Result, Usage
from llmbus.store import Store

_log = logging.getLogger("llmbus.worker")

# Injected effects. The callback sender POSTs the Result payload to a URL; the
# timeout runner bounds one provider call. Both real implementations (httpx,
# asyncio.wait_for) are integration concerns, so they are injected and defaulted
# — tests pass fakes and never touch the network or the wall clock.
CallbackSender = Callable[[str, dict[str, Any]], Awaitable[None]]
TimeoutRunner = Callable[[Awaitable[ProviderResult], float], Awaitable[ProviderResult]]


async def _default_apply_timeout(
    call: Awaitable[ProviderResult], timeout_s: float
) -> ProviderResult:
    """Real per-attempt timeout: cancel the provider call after `timeout_s` (a
    `TimeoutError`, which `is_retryable` treats as transient)."""
    return await asyncio.wait_for(call, timeout_s)


@dataclasses.dataclass(frozen=True)
class WorkerDeps:
    """Everything `process_job` needs, injected as one bundle.

    Bundled (not passed positionally) so `process_job` stays a two-argument
    function and each collaborator — store, providers, rate limiter, callback
    sender, and the sleep/RNG/timeout effects — has exactly one wiring point.
    `sleep`, `rand`, and `apply_timeout` default to the real effects; tests
    override them so no real time passes and no network is touched.
    """

    providers: Mapping[str, Provider]
    rate_limiter: RateLimiter
    store: Store
    policy: WorkerPolicy
    deliver_callback: CallbackSender
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    rand: Callable[[], float] = random.random
    apply_timeout: TimeoutRunner = _default_apply_timeout


def estimate_tokens(job: Job, default_output_tokens: int) -> int:
    """Pre-call token estimate for the rate limiter (§6).

    Input side: ~4 characters per token over all message content (the standard
    rough heuristic; the token bucket self-corrects next cycle, so precision is
    not critical). Output side: the caller's `max_tokens` if set, else
    `default_output_tokens`. Reserved *before* the call because the limiter must
    throttle ahead of the request to avoid a provider 429, not after it.
    """
    input_chars = sum(len(message.content) for message in job.messages)
    input_tokens = input_chars // 4
    max_tokens = job.params.max_tokens
    output_tokens = max_tokens if max_tokens is not None else default_output_tokens
    return input_tokens + output_tokens


def _describe(exc: BaseException) -> str:
    """One-line error string stored on a failed `Result` (type + message)."""
    return f"{type(exc).__name__}: {exc}"


def result_ok(job: Job, provider_name: str, call: ProviderResult, cost: float) -> Result:
    """Build the terminal success `Result`, folding the computed cost into a new
    `Usage` (the provider's is frozen with `cost_usd=0.0`, §7) and echoing the
    job's `meta` back untouched (§4)."""
    return Result(
        job_id=job.job_id,
        status="ok",
        completion=call.completion,
        usage=Usage(
            input_tokens=call.usage.input_tokens,
            output_tokens=call.usage.output_tokens,
            cost_usd=cost,
        ),
        provider=provider_name,
        meta=job.meta,
    )


def result_error(job: Job, provider_name: str | None, error: str) -> Result:
    """Build the terminal error `Result` (no usage/cost — the call never
    succeeded), echoing the job's `meta` back untouched (§4)."""
    return Result(
        job_id=job.job_id,
        status="error",
        provider=provider_name,
        error=error,
        meta=job.meta,
    )


def _price(job: Job, call: ProviderResult) -> float:
    """USD cost of a successful call at the rate in force on the job's date (§6).

    `Decimal` from `cost.py` is narrowed to `float` here — the schema/store
    boundary — exactly where the ledger stops needing exact arithmetic.
    """
    cost = cost_usd(
        job.model, call.usage.input_tokens, call.usage.output_tokens, job.submitted_at.date()
    )
    return float(cost)


async def _call_with_retry(deps: WorkerDeps, job: Job, provider: Provider, estimate: int) -> Result:
    """Call the provider, retrying transient failures with jittered backoff (§6).

    Re-reserves rate-limit budget before every attempt (a retry is another real
    request against the provider's quota) and applies the per-attempt timeout.
    Returns a terminal `Result` — the success on the first good call, or the last
    error once retries are exhausted or the failure is non-retryable.
    """
    retry = deps.policy.retry
    attempt = 0
    # `while True` (not `range`) so both exits are `return`s — no unreachable
    # post-loop statement for the type checker to demand and the mutation gate to
    # leave as a survivor. `attempt` strictly increases and the guard bounds it at
    # `max_attempts`, so this always terminates.
    while True:
        await deps.rate_limiter.acquire(provider.name, estimate)
        try:
            call = await deps.apply_timeout(
                provider.call(job.model, job.messages, job.params),
                deps.policy.job_timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 - classify ANY provider/SDK failure, then decide retry vs terminal
            if is_retryable(exc) and attempt + 1 < retry.max_attempts:
                await deps.sleep(backoff_delay(attempt, retry, deps.rand()))
                attempt += 1
                continue
            return result_error(job, provider.name, _describe(exc))
        return result_ok(job, provider.name, call, _price(job, call))


# Callback signing (§14 #19). The worker POSTs the `Result` to a producer's
# `callback_url`; when a shared secret is configured it signs the *raw body* so the
# receiver can trust the sender. Header name + `sha256=<hex>` shape mirror the
# hate-moderator Meta-webhook signature exactly, so its receiver verifies with the
# same `hmac.compare_digest(f"sha256={...}", header)` it already runs. Pure logic
# (in the mutation gate); the httpx POST that applies these lives in
# `worker.make_callback_sender` and puts the *same* bytes on the wire.
CALLBACK_SIGNATURE_HEADER = "X-Llmbus-Signature-256"


def callback_signature(secret: str, body: bytes) -> str:
    """HMAC-SHA256 of the raw callback body, formatted `sha256=<hexdigest>` (§14 #19)."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def callback_headers(secret: str | None, body: bytes) -> dict[str, str]:
    """Headers for a callback POST: JSON content-type, plus an HMAC signature over
    `body` when a secret is set (§14 #19).

    No secret → no signature header: delivery stays unauthenticated, the v1 default
    for a localhost-only callback. `secret is None` is the off switch (config never
    yields an empty-but-present secret — `parse_config` maps blank to `None`), so a
    configured secret always signs.
    """
    headers = {"Content-Type": "application/json"}
    if secret is not None:
        headers[CALLBACK_SIGNATURE_HEADER] = callback_signature(secret, body)
    return headers


async def _deliver(deps: WorkerDeps, url: str, result: Result) -> None:
    """POST the result to the callback URL, best-effort (§3).

    A callback failure is logged and swallowed — the result is already durably in
    the store, so poll remains the reliable delivery path (§11) and callback
    retry/dead-letter is deferred to v2 (§13).
    """
    try:
        await deps.deliver_callback(url, result.model_dump(by_alias=True))
    except Exception as exc:  # noqa: BLE001 - callback is best-effort; the store/poll path is reliable
        _log.warning("callback POST to %s failed for job %s: %s", url, result.job_id, exc)


async def process_job(deps: WorkerDeps, job: Job) -> Result:
    """Process one job end-to-end and return its terminal `Result` (§6).

    Routes the model to a provider, runs the rate-limited retry loop, finalizes
    the store one-shot (idempotent under redelivery), and — only when *this*
    delivery won the finalize and the job wants one — fires the callback. An
    unknown model or an unwired provider becomes an error `Result` so one bad job
    never stalls the loop.
    """
    try:
        provider_name = provider_for(job.model)
    except UnknownModelError:
        result = result_error(job, None, f"no provider serves model {job.model!r}")
    else:
        provider = deps.providers.get(provider_name)
        if provider is None:
            result = result_error(
                job, provider_name, f"no adapter configured for provider {provider_name!r}"
            )
        else:
            result = await _call_with_retry(
                deps, job, provider, estimate_tokens(job, deps.policy.default_output_tokens)
            )
    finalized = await deps.store.finalize(result)
    if finalized and job.callback_url is not None:
        await _deliver(deps, job.callback_url, result)
    return result
