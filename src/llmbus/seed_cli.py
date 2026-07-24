"""`llmbus-seed-policy` — upsert the model-policy seed into the store.

The impure shell around ``policy_seed.py`` (the same pure/shell split ``dashboard.py``
/ ``cli.py`` use): open the store, upsert each seed row, report. Model parity so a kind
flipped onto the bus keeps the model it runs on today (§14 #23). Like the cost CLI, this
bridges to the async bus at its own ``asyncio.run`` edge (§14 #17) and touches only the
SQLite store — no Iggy connection, no provider SDKs, no API keys.

Idempotent: ``set_model_policy`` upserts, so re-running only refreshes ``updated_at``.
It refuses a seed row the registry can't route (which would fail loud only later, at a
producer's first bus job) and refuses a missing store (which ``Store.connect()`` would
otherwise create, leaving a stray database the worker never reads) — the same fail-loud
guard the cost CLI uses.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from llmbus.cli import require_existing_store, resolve_store_path
from llmbus.config import ConfigError
from llmbus.policy_seed import MODEL_POLICY_SEED, PolicySeed
from llmbus.providers.base import UnknownModelError, provider_for
from llmbus.store import Store


def unroutable(seed: Sequence[PolicySeed] = MODEL_POLICY_SEED) -> list[PolicySeed]:
    """Seed rows whose model the bus does not route.

    Must be empty before applying: a policy pointing at an unknown model resolves
    fine here but fails loud at ``submit`` (§14 #23), so it seeds a landmine, not
    model parity. Catch it at the source instead.
    """
    bad: list[PolicySeed] = []
    for row in seed:
        try:
            provider_for(row.model)
        except UnknownModelError:
            bad.append(row)
    return bad


async def apply_seed(
    store: Store, seed: Sequence[PolicySeed] = MODEL_POLICY_SEED
) -> list[PolicySeed]:
    """Upsert every seed row into ``store``; return the rows applied."""
    for row in seed:
        await store.set_model_policy(row.project, row.kind, row.model)
    return list(seed)


async def _seed_store(store_path: str, seed: Sequence[PolicySeed]) -> list[PolicySeed]:
    async with Store(store_path) as store:
        return await apply_seed(store, seed)


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface. The store path is overridable; nothing is hardcoded (§10)."""
    parser = argparse.ArgumentParser(
        prog="llmbus-seed-policy",
        description="Upsert the central model-policy seed (model parity for bused kinds).",
    )
    parser.add_argument(
        "--store-path",
        default=None,
        help="SQLite store to write (default: STORE_PATH from .env/environment).",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `llmbus-seed-policy`. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    bad = unroutable()
    if bad:
        names = ", ".join(f"{r.project}/{r.kind}->{r.model}" for r in bad)
        print(f"llmbus-seed-policy: unroutable model(s) in seed: {names}", file=sys.stderr)
        return 2
    try:
        store_path = resolve_store_path(args.store_path)
        require_existing_store(store_path)
    except ConfigError as error:
        print(f"llmbus-seed-policy: {error}", file=sys.stderr)
        return 2
    applied = asyncio.run(_seed_store(store_path, MODEL_POLICY_SEED))
    for row in applied:
        print(f"seeded {row.project}/{row.kind} -> {row.model}")
    print(f"llmbus-seed-policy: {len(applied)} policy row(s) upserted into {store_path}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
