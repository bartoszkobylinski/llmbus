"""Render the cost ledger as a self-contained HTML page (ARCHITECTURE.md §11).

`Store.cost_by_project_day()` is the ledger; this module is the *view* over it.
Everything here is pure — rows in, HTML string out — so the whole rendering
surface unit-tests without SQLite, without a clock, and without a browser, and
sits in the mutation gate alongside `cost.py`/`retry.py`. The impure half (open
the store, read the rows, write the file) lives in `cli.py`, mirroring the
`worker-core` / `worker-loop` split of §6.

Three decisions worth stating, because each is a trade and not an obvious default:

- **Money is re-lifted to `Decimal`.** The store hands back `float` (SQLite
  `SUM`), but `cost.py` keeps money in `Decimal` precisely so a ledger doesn't
  accumulate float error. Summing project/day totals here would reintroduce
  exactly that error, so each row is converted via `Decimal(str(...))` — through
  the *shortest repr*, never `Decimal(float)`, which would preserve the binary
  noise instead of the decimal value the store meant.
- **Six decimal places, everywhere.** Per-job spend on this bus is sub-cent (a
  smoke job costs ~$0.00006), so the conventional 2dp would render the entire
  ledger as `$0.00` — a view that reports nothing. One fixed precision, used for
  every figure, keeps a column of numbers comparable at a glance.
- **Days are bars, not a line.** The ledger is `HAVING SUM(cost_usd) > 0`, so
  days with no spend are absent rather than zero. Joining those points with a
  line would draw a slope across a gap that isn't in the data; discrete bars
  state only what was recorded.

The page is standalone by construction: inline CSS, no scripts, no fonts, no
network. It renders from a `file://` path, survives `scp`, and reads correctly in
both light and dark. Every project name reaching the page is HTML-escaped — a
`project` is producer-supplied (§4), so it is untrusted text, not a literal.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from llmbus.store import ProjectDayCost

# Bar hue, categorical slot 1, one hue for every bar: `project` and `day` are
# nominal keys, so shading a bar by its own magnitude would double-encode the
# length the bar already shows. Light/dark are the same hue stepped for each
# surface. Both steps validated (lightness band, chroma floor, >=3:1 contrast)
# against their surface.
_SERIES_LIGHT = "#2a78d6"
_SERIES_DARK = "#3987e5"

_MONEY_PLACES = 6


@dataclass(frozen=True)
class Total:
    """One aggregated bucket of the ledger: `key` (a project or a day) and its USD."""

    key: str
    amount: Decimal


@dataclass(frozen=True)
class CostSummary:
    """The ledger reduced to everything the page draws.

    Built by `summarize()` so the reduction is testable on its own, separate from
    the HTML. `by_project` is ordered by spend (the question a cost view is opened
    to answer: who spends the most); `by_day` stays chronological, because a
    time axis reordered by magnitude is unreadable.
    """

    rows: tuple[ProjectDayCost, ...]
    by_project: tuple[Total, ...]
    by_day: tuple[Total, ...]
    grand_total: Decimal


def to_decimal(value: float) -> Decimal:
    """Lift a store `float` to `Decimal` through its shortest repr.

    `Decimal(0.1)` is `0.1000000000000000055511151231257827…` — the binary double,
    faithfully expanded. `Decimal(str(0.1))` is `0.1`, the number the ledger
    actually meant. Totals are summed after this conversion, so the page's
    arithmetic matches `cost.py`'s rather than drifting from it.
    """
    return Decimal(str(value))


def format_usd(amount: Decimal) -> str:
    """Format USD at fixed 6dp with thousands separators, e.g. `$1,234.567890`.

    Fixed precision rather than adaptive: per-job cost here is sub-cent, and a
    column mixing `$0.000060` with `$1.23` cannot be scanned vertically.
    """
    return f"${amount:,.{_MONEY_PLACES}f}"


def bar_width_pct(amount: Decimal, largest: Decimal) -> float:
    """`amount` as a percentage of `largest`, clamped to 0–100.

    `largest <= 0` yields 0.0 rather than dividing: an all-zero (or empty) ledger
    must render as bars of no length, not raise. The clamp is a `max`/`min` pair
    rather than guard branches so there is no boundary `if` for a mutant to flip
    into an equivalent form.
    """
    if largest <= 0:
        return 0.0
    return max(0.0, min(100.0, float(amount / largest) * 100))


def _sum_by(rows: Sequence[ProjectDayCost], attribute: str) -> dict[str, Decimal]:
    """Total USD per distinct value of `attribute` (`project` or `day`)."""
    totals: dict[str, Decimal] = {}
    for row in rows:
        key: str = getattr(row, attribute)
        totals[key] = totals.get(key, Decimal(0)) + to_decimal(row.cost_usd)
    return totals


def summarize(rows: Sequence[ProjectDayCost]) -> CostSummary:
    """Reduce ledger rows to per-project, per-day, and grand totals.

    Ties in `by_project` fall back to the project name so the ordering is total,
    not merely sorted — two projects with identical spend must not swap places
    between runs of the same data.
    """
    per_project = _sum_by(rows, "project")
    per_day = _sum_by(rows, "day")
    by_project = tuple(
        Total(key, amount)
        for key, amount in sorted(per_project.items(), key=lambda item: (-item[1], item[0]))
    )
    by_day = tuple(Total(key, per_day[key]) for key in sorted(per_day))
    return CostSummary(
        rows=tuple(rows),
        by_project=by_project,
        by_day=by_day,
        grand_total=sum(per_project.values(), Decimal(0)),
    )


def _esc(value: str) -> str:
    """HTML-escape untrusted text (project names, paths), quotes included.

    `quote=True` is `html.escape`'s default and is left implicit: passing it
    explicitly reads as a safety choice but is a no-op, so it can only appear in
    the mutation gate as a mutant no test can kill. Quote escaping matters here —
    project names are interpolated into `title="…"` attributes.
    """
    return html.escape(value)


def _bar_rows(totals: Sequence[Total]) -> str:
    """The bar rows of one chart: label, bar, value at the tip.

    The label sits in its own column and the value past the bar's end, so neither
    can be clipped by a short bar — the failure mode that makes in-bar labels
    unreadable at exactly the values a cost view cares about (the small ones).
    """
    # No `default=` fallback: `_chart` already refuses an empty series, so a
    # default would be a value no test could ever observe — an equivalent mutant
    # by construction. Callers guarantee non-empty.
    largest = max(total.amount for total in totals)
    cells = []
    for total in totals:
        width = bar_width_pct(total.amount, largest)
        label, value = _esc(total.key), format_usd(total.amount)
        cells.append(
            f'<div class="row" title="{label}: {value}">'
            f'<div class="row-label">{label}</div>'
            f'<div class="track"><div class="bar" style="width:{width:.4f}%"></div></div>'
            f'<div class="row-value">{value}</div>'
            f"</div>"
        )
    return "\n".join(cells)


def _chart(title: str, caption: str, totals: Sequence[Total]) -> str:
    """One titled chart card. A single series, so it carries no legend box."""
    if not totals:
        return ""
    return (
        f'<section class="card">'
        f"<h2>{_esc(title)}</h2>"
        f'<p class="caption">{_esc(caption)}</p>'
        f'<div class="chart">{_bar_rows(totals)}</div>'
        f"</section>"
    )


def _tiles(summary: CostSummary) -> str:
    """The KPI row: counts, plus the single largest project and day."""
    # Both guards are explicit `if` rather than `max(default=...)` for the same
    # reason: an unobservable default is a mutant nothing can kill. `max` returns
    # the first of equal values, so a tie between days resolves to the earliest.
    top_project = summary.by_project[0] if summary.by_project else None
    peak_day = max(summary.by_day, key=lambda total: total.amount) if summary.by_day else None
    tiles = [
        ("Projects", str(len(summary.by_project))),
        ("Days with spend", str(len(summary.by_day))),
        ("Top project", _esc(top_project.key) if top_project else "—"),
        ("Peak day", _esc(peak_day.key) if peak_day else "—"),
    ]
    cells = [
        f'<div class="tile"><div class="tile-label">{label}</div>'
        f'<div class="tile-value">{value}</div></div>'
        for label, value in tiles
    ]
    return f'<div class="tiles">{"".join(cells)}</div>'


def _table(summary: CostSummary) -> str:
    """The table view: every project x day cell, plus per-day and grand totals.

    Present unconditionally, not as a fallback. It is the accessible twin of the
    charts — every value the bars encode by length is readable here as a number,
    so nothing is gated behind colour, length, or hover.
    """
    if not summary.rows:
        return ""
    projects = [total.key for total in summary.by_project]
    cells = {(row.project, row.day): to_decimal(row.cost_usd) for row in summary.rows}
    head = "".join(f"<th>{_esc(project)}</th>" for project in projects)
    body = []
    for day in summary.by_day:
        values = "".join(
            f"<td>{format_usd(cells[(project, day.key)]) if (project, day.key) in cells else '—'}</td>"
            for project in projects
        )
        body.append(f"<tr><th>{_esc(day.key)}</th>{values}<td>{format_usd(day.amount)}</td></tr>")
    totals = "".join(f"<td>{format_usd(total.amount)}</td>" for total in summary.by_project)
    foot = f"<tr><th>Total</th>{totals}<td>{format_usd(summary.grand_total)}</td></tr>"
    return (
        f'<section class="card"><h2>Every project and day</h2>'
        f'<p class="caption">The same figures the charts encode, as numbers.</p>'
        f'<div class="scroll"><table><thead><tr><th>Day</th>{head}<th>Total</th></tr></thead>'
        f"<tbody>{''.join(body)}</tbody><tfoot>{foot}</tfoot></table></div></section>"
    )


def _empty_note(summary: CostSummary) -> str:
    """Say plainly that an empty ledger is a real state, not a broken page."""
    if summary.rows:
        return ""
    return (
        '<section class="card"><h2>No spend recorded yet</h2>'
        '<p class="caption">The ledger counts only jobs that completed with a non-zero '
        "cost. A store holding only pending, failed, or zero-cost jobs is empty here "
        "and that is correct — it is not an error.</p></section>"
    )


_STYLE = f"""
:root {{
  color-scheme: light;
  --surface: #fcfcfb; --plane: #f9f9f7;
  --ink: #0b0b0b; --ink-2: #52514e; --muted: #898781;
  --grid: #e1e0d9; --axis: #c3c2b7; --ring: rgba(11,11,11,0.10);
  --series: {_SERIES_LIGHT};
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    color-scheme: dark;
    --surface: #1a1a19; --plane: #0d0d0d;
    --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
    --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
    --series: {_SERIES_DARK};
  }}
}}
:root[data-theme="dark"] {{
  color-scheme: dark;
  --surface: #1a1a19; --plane: #0d0d0d;
  --ink: #ffffff; --ink-2: #c3c2b7; --muted: #898781;
  --grid: #2c2c2a; --axis: #383835; --ring: rgba(255,255,255,0.10);
  --series: {_SERIES_DARK};
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 2rem 1.25rem 4rem;
  background: var(--plane); color: var(--ink);
  font-family: system-ui, -apple-system, "Segoe UI", sans-serif;
  line-height: 1.5;
}}
main {{ max-width: 60rem; margin: 0 auto; }}
h1 {{ font-size: 1.125rem; font-weight: 600; margin: 0; letter-spacing: 0.01em; }}
h2 {{ font-size: 0.9375rem; font-weight: 600; margin: 0; }}
.meta, .caption {{ color: var(--muted); font-size: 0.8125rem; margin: 0.25rem 0 0; }}
.meta code {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }}
.hero {{ margin: 1.75rem 0 0; }}
.hero-label {{ color: var(--ink-2); font-size: 0.8125rem; }}
.hero-value {{ font-size: 3rem; font-weight: 600; line-height: 1.1; margin-top: 0.125rem; }}
.tiles {{
  display: grid; gap: 0.75rem; margin-top: 1.5rem;
  grid-template-columns: repeat(auto-fit, minmax(9rem, 1fr));
}}
.tile {{
  background: var(--surface); border: 1px solid var(--ring);
  border-radius: 10px; padding: 0.75rem 0.875rem;
}}
.tile-label {{ color: var(--muted); font-size: 0.75rem; }}
.tile-value {{ font-size: 1.125rem; font-weight: 600; margin-top: 0.125rem;
  overflow-wrap: anywhere; }}
.card {{
  background: var(--surface); border: 1px solid var(--ring); border-radius: 12px;
  padding: 1.125rem 1.25rem 1.25rem; margin-top: 1rem;
}}
.chart {{ margin-top: 1rem; display: flex; flex-direction: column; gap: 0.625rem; }}
.row {{
  display: grid; align-items: center; gap: 0.75rem;
  grid-template-columns: minmax(0, 9rem) 1fr minmax(0, auto);
}}
.row-label {{ font-size: 0.8125rem; color: var(--ink-2); overflow-wrap: anywhere; }}
.track {{ border-left: 1px solid var(--axis); padding-left: 2px; }}
.bar {{
  height: 18px; background: var(--series);
  border-radius: 0 4px 4px 0; min-width: 2px;
}}
.row-value {{
  font-size: 0.8125rem; color: var(--ink-2);
  font-variant-numeric: tabular-nums; white-space: nowrap;
}}
.scroll {{ overflow-x: auto; margin-top: 1rem; }}
table {{ border-collapse: collapse; width: 100%; font-size: 0.8125rem; }}
th, td {{
  text-align: right; padding: 0.4375rem 0.75rem;
  border-bottom: 1px solid var(--grid); white-space: nowrap;
}}
thead th {{ color: var(--muted); font-weight: 500; }}
/* The day column is left-aligned in the body, so its header must be too — a
   right-aligned "Day" over left-aligned dates reads as a column boundary. */
thead th:first-child {{ text-align: left; }}
tbody th, tfoot th {{ text-align: left; font-weight: 500; color: var(--ink-2); }}
td {{ font-variant-numeric: tabular-nums; }}
tfoot th, tfoot td {{ font-weight: 600; color: var(--ink); border-bottom: none;
  border-top: 1px solid var(--axis); }}
footer {{ color: var(--muted); font-size: 0.75rem; margin-top: 1.5rem; }}
"""


def render_dashboard(summary: CostSummary, generated_at: datetime, store_path: str) -> str:
    """Render the whole ledger as one standalone HTML document.

    `generated_at` and `store_path` are injected rather than read here: a pure
    renderer means the page is byte-for-byte reproducible in a test, and the
    stamp says *when the data was read*, not when someone opened the file.
    """
    stamp = generated_at.isoformat(timespec="seconds")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>llmbus — cost ledger</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
<header>
<h1>llmbus — cost ledger</h1>
<p class="meta">Generated {_esc(stamp)} from <code>{_esc(store_path)}</code></p>
</header>
<div class="hero">
<div class="hero-label">Total spend</div>
<div class="hero-value">{format_usd(summary.grand_total)}</div>
</div>
{_tiles(summary)}
{_empty_note(summary)}
{_chart("Spend by project", "Total USD per project, largest first.", summary.by_project)}
{_chart("Spend by day", "Total USD per day the bus recorded spend.", summary.by_day)}
{_table(summary)}
<footer>
Costs are priced at the rate in force on each job's submission date (cost.py).
Days without recorded spend are absent, not zero.
</footer>
</main>
</body>
</html>
"""
