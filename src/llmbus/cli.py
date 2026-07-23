"""`llmbus-costs` — write the cost ledger to a standalone HTML file (§11).

The impure half of the cost view: open the store, read the ledger, stamp the
clock, write the file. All the rendering logic lives in `dashboard.py` as pure
functions, so this module stays a thin shell — the same `worker-core` /
`worker-loop` split §6 uses, and the reason `dashboard.py` can sit in the
mutation gate while this cannot.

`asyncio.run` here is not a sync wrapper in the sense §1 forbids. The bus stays
async end-to-end; this is a short-lived process bridging to it at *its own*
edge — the shape §14 #17 settled for the cron `drain_queue` path. Nothing
long-lived and nothing inside the library does this.

The report deliberately reads **only** the SQLite file: no Iggy connection, no
provider SDKs, no API keys (`config.parse_store_path`). It is safe to run on a
box where the worker is down, and it cannot perturb the running worker — WAL
means this reader never blocks that writer (§11).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

from llmbus.config import ConfigError, load_store_path
from llmbus.dashboard import CostSummary, format_usd, render_dashboard, summarize
from llmbus.store import Store

_DEFAULT_OUTPUT = "llmbus-costs.html"


async def collect_summary(store_path: str) -> CostSummary:
    """Open the store read-only-in-practice, reduce the ledger, close it."""
    async with Store(store_path) as store:
        return summarize(await store.cost_by_project_day())


def build_parser() -> argparse.ArgumentParser:
    """The CLI surface. Both paths are overridable; neither is hardcoded (§10)."""
    parser = argparse.ArgumentParser(
        prog="llmbus-costs",
        description="Render the llmbus cost ledger as a standalone HTML page.",
    )
    parser.add_argument(
        "--store-path",
        default=None,
        help="SQLite results store to read (default: STORE_PATH from .env/environment).",
    )
    parser.add_argument(
        "--output",
        default=_DEFAULT_OUTPUT,
        help=f"HTML file to write (default: {_DEFAULT_OUTPUT}).",
    )
    return parser


def resolve_store_path(explicit: str | None) -> str:
    """Use the explicit `--store-path` when given, else `STORE_PATH` from the env."""
    if explicit is not None:
        return explicit
    return load_store_path()


def require_existing_store(store_path: str) -> None:
    """Fail when the store file is absent, instead of reporting an empty ledger.

    `Store.connect()` creates the file and schema if missing — correct for the
    worker, wrong here: a typo'd path would otherwise produce a clean, confident
    `$0.000000` page and leave a stray empty database behind. A cost view that
    silently reports zero because it read the wrong file is worse than one that
    refuses, so this checks first (§ fail-loud).
    """
    if store_path != ":memory:" and not Path(store_path).exists():
        raise ConfigError(f"no store at {store_path!r} — nothing to report on")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point for `llmbus-costs`. Returns a process exit code."""
    args = build_parser().parse_args(argv)
    try:
        store_path = resolve_store_path(args.store_path)
        require_existing_store(store_path)
    except ConfigError as error:
        print(f"llmbus-costs: {error}", file=sys.stderr)
        return 2
    summary = asyncio.run(collect_summary(store_path))
    output = Path(args.output)
    # No special case for a bare filename: its parent is `Path(".")`, which
    # `mkdir(parents=True, exist_ok=True)` accepts unchanged.
    output.parent.mkdir(parents=True, exist_ok=True)
    page = render_dashboard(summary, datetime.now(UTC), store_path)
    output.write_text(page, encoding="utf-8")
    print(f"wrote {output} — {format_usd(summary.grand_total)} across {len(summary.rows)} rows")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
