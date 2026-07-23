"""Unit tests for the cost-ledger HTML view (§11).

dashboard.py is pure — rows in, HTML string out — so everything here runs with no
SQLite, no clock, and no browser. It is in the mutmut gate, so these assert exact
strings and exact numbers rather than "contains something": a mutant that shifts a
rate, a precision, or a sort direction has to fail an assertion, not merely look
different.
"""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from llmbus.dashboard import (
    CostSummary,
    Total,
    _bar_rows,
    _chart,
    _empty_note,
    _esc,
    _table,
    _tiles,
    bar_width_pct,
    format_usd,
    render_dashboard,
    summarize,
    to_decimal,
)
from llmbus.store import ProjectDayCost

_GENERATED = datetime(2026, 7, 23, 9, 30, 15, tzinfo=timezone.utc)


def _row(project="hate-moderator", day="2026-07-03", cost=1.0):
    return ProjectDayCost(project, day, cost)


# --- to_decimal: money must not inherit float noise -------------------------


def test_to_decimal_goes_through_the_shortest_repr_not_the_binary_double():
    # Decimal(0.1) would be 0.1000000000000000055511151231257827…; the store meant 0.1.
    assert to_decimal(0.1) == Decimal("0.1")


def test_to_decimal_sums_without_float_drift():
    # 0.1 + 0.2 != 0.3 in float; the ledger must not inherit that.
    assert to_decimal(0.1) + to_decimal(0.2) == Decimal("0.3")


# --- format_usd -------------------------------------------------------------


def test_format_usd_uses_six_places_so_sub_cent_spend_is_visible():
    assert format_usd(Decimal("0.00006")) == "$0.000060"


def test_format_usd_groups_thousands():
    assert format_usd(Decimal("1234.5")) == "$1,234.500000"


def test_format_usd_renders_zero_at_full_precision():
    assert format_usd(Decimal(0)) == "$0.000000"


def test_format_usd_rounds_beyond_six_places_rather_than_truncating():
    assert format_usd(Decimal("0.0000005")) == "$0.000000"
    assert format_usd(Decimal("0.0000015")) == "$0.000002"


# --- bar_width_pct ----------------------------------------------------------


def test_bar_width_pct_is_a_true_ratio_not_a_product():
    # amount/largest = 25%; a `*` mutant would give 400 (clamped to 100).
    assert bar_width_pct(Decimal(1), Decimal(4)) == 25.0


def test_bar_width_pct_scales_to_a_hundred_at_the_largest_value():
    assert bar_width_pct(Decimal(4), Decimal(4)) == 100.0


def test_bar_width_pct_half_is_fifty():
    assert bar_width_pct(Decimal("0.5"), Decimal(1)) == 50.0


def test_bar_width_pct_clamps_above_the_largest_to_exactly_one_hundred():
    assert bar_width_pct(Decimal(2), Decimal(1)) == 100.0


def test_bar_width_pct_of_zero_is_zero_not_a_floor():
    assert bar_width_pct(Decimal(0), Decimal(1)) == 0.0


def test_bar_width_pct_returns_zero_for_an_empty_ledger_instead_of_dividing():
    assert bar_width_pct(Decimal(0), Decimal(0)) == 0.0


def test_bar_width_pct_still_divides_when_largest_is_one():
    # Pins the guard at `<= 0`: a `<= 1` mutant would zero this out.
    assert bar_width_pct(Decimal("0.25"), Decimal(1)) == 25.0


# --- summarize --------------------------------------------------------------


def test_summarize_empty_ledger_is_all_empty_and_zero():
    summary = summarize([])
    assert summary.rows == ()
    assert summary.by_project == ()
    assert summary.by_day == ()
    assert summary.grand_total == Decimal(0)


def test_summarize_grand_total_is_a_decimal_even_when_the_ledger_is_empty():
    # `sum()` without an explicit Decimal start returns int 0 here, which compares
    # equal to Decimal(0) and would slip past the assertion above. Money stays
    # Decimal on every path, so the type is part of the contract.
    assert isinstance(summarize([]).grand_total, Decimal)


def test_summarize_totals_a_project_across_days():
    summary = summarize([_row(cost=1.5, day="2026-07-03"), _row(cost=2.25, day="2026-07-04")])
    assert summary.by_project == (Total("hate-moderator", Decimal("3.75")),)


def test_summarize_totals_a_day_across_projects():
    summary = summarize([_row(project="a", cost=1.0), _row(project="b", cost=0.5)])
    assert summary.by_day == (Total("2026-07-03", Decimal("1.5")),)


def test_summarize_orders_projects_by_spend_descending():
    # Names deliberately run *against* the spend order: alphabetically "alpha"
    # precedes "beta", so a sort that lost its key would produce the reverse.
    summary = summarize(
        [_row(project="alpha", cost=1.0), _row(project="beta", cost=9.0)],
    )
    assert [total.key for total in summary.by_project] == ["beta", "alpha"]


def test_summarize_breaks_project_ties_by_name_for_a_stable_order():
    summary = summarize([_row(project="zeta", cost=2.0), _row(project="alpha", cost=2.0)])
    assert [total.key for total in summary.by_project] == ["alpha", "zeta"]


def test_summarize_orders_days_chronologically_not_by_spend():
    summary = summarize(
        [_row(day="2026-07-05", cost=9.0), _row(day="2026-07-04", cost=1.0)],
    )
    assert [total.key for total in summary.by_day] == ["2026-07-04", "2026-07-05"]


def test_summarize_grand_total_is_the_sum_of_every_row():
    summary = summarize([_row(cost=1.0), _row(project="b", cost=0.25)])
    assert summary.grand_total == Decimal("1.25")


def test_summarize_grand_total_of_one_row_carries_no_offset():
    assert summarize([_row(cost=2.0)]).grand_total == Decimal(2)


def test_summarize_keeps_the_original_rows():
    rows = [_row(cost=1.0)]
    assert summarize(rows).rows == (rows[0],)


# --- escaping: `project` is producer-supplied, so it is untrusted ------------


def test_esc_escapes_angle_brackets():
    assert _esc("<script>") == "&lt;script&gt;"


def test_esc_escapes_quotes_because_values_land_in_attributes():
    assert _esc('a"b') == "a&quot;b"


def test_a_project_name_cannot_inject_markup_into_the_page():
    summary = summarize([_row(project="<img src=x onerror=alert(1)>", cost=1.0)])
    page = render_dashboard(summary, _GENERATED, "/tmp/store.db")
    assert "<img src=x" not in page
    assert "&lt;img src=x onerror=alert(1)&gt;" in page


def test_a_hostile_project_name_cannot_break_out_of_the_title_attribute():
    summary = summarize([_row(project='" onmouseover="evil()', cost=1.0)])
    page = render_dashboard(summary, _GENERATED, "/tmp/store.db")
    assert 'onmouseover="evil()' not in page


def test_the_store_path_is_escaped_too():
    page = render_dashboard(summarize([]), _GENERATED, "/tmp/<b>.db")
    assert "/tmp/&lt;b&gt;.db" in page


# --- bar rows ---------------------------------------------------------------


def test_bar_rows_scales_widths_against_the_largest_value():
    rows = _bar_rows([Total("big", Decimal(4)), Total("small", Decimal(1))])
    assert "width:100.0000%" in rows
    assert "width:25.0000%" in rows


def test_bar_rows_labels_each_bar_and_puts_the_value_at_the_tip():
    rows = _bar_rows([Total("alpha", Decimal("1.5"))])
    assert '<div class="row-label">alpha</div>' in rows
    assert '<div class="row-value">$1.500000</div>' in rows


def test_bar_rows_carries_a_hover_title_that_repeats_the_value():
    rows = _bar_rows([Total("alpha", Decimal("1.5"))])
    assert 'title="alpha: $1.500000"' in rows


def test_bar_rows_emits_one_row_per_total():
    rows = _bar_rows([Total("a", Decimal(1)), Total("b", Decimal(2))])
    assert rows.count('class="row"') == 2


def test_bar_rows_markup_is_exact():
    # One equality pins the whole row shape — element order, the bar nested in its
    # track, the width format, and the newline between rows.
    assert _bar_rows([Total("big", Decimal(4)), Total("small", Decimal(1))]) == (
        '<div class="row" title="big: $4.000000">'
        '<div class="row-label">big</div>'
        '<div class="track"><div class="bar" style="width:100.0000%"></div></div>'
        '<div class="row-value">$4.000000</div>'
        "</div>"
        "\n"
        '<div class="row" title="small: $1.000000">'
        '<div class="row-label">small</div>'
        '<div class="track"><div class="bar" style="width:25.0000%"></div></div>'
        '<div class="row-value">$1.000000</div>'
        "</div>"
    )


# --- chart cards ------------------------------------------------------------


def test_chart_renders_its_title_and_caption():
    card = _chart("Spend by project", "Largest first.", [Total("a", Decimal(1))])
    assert "<h2>Spend by project</h2>" in card
    assert '<p class="caption">Largest first.</p>' in card


def test_chart_of_an_empty_series_renders_nothing_at_all():
    assert _chart("Spend by project", "Largest first.", []) == ""


def test_chart_carries_no_legend_because_it_is_a_single_series():
    card = _chart("Spend by project", "Largest first.", [Total("a", Decimal(1))])
    assert "legend" not in card.lower()


# --- KPI tiles --------------------------------------------------------------


def test_tiles_count_projects_and_days():
    summary = summarize(
        [_row(project="a", day="2026-07-03"), _row(project="b", day="2026-07-04")],
    )
    tiles = _tiles(summary)
    assert '<div class="tile-label">Projects</div><div class="tile-value">2</div>' in tiles
    assert '<div class="tile-label">Days with spend</div><div class="tile-value">2</div>' in tiles


def test_tiles_name_the_biggest_spender_not_merely_the_first_row():
    summary = summarize([_row(project="small", cost=1.0), _row(project="big", cost=9.0)])
    assert '<div class="tile-label">Top project</div><div class="tile-value">big</div>' in _tiles(
        summary
    )


def test_tiles_name_the_costliest_day_not_the_latest():
    summary = summarize(
        [_row(day="2026-07-03", cost=9.0), _row(day="2026-07-09", cost=1.0)],
    )
    assert '<div class="tile-value">2026-07-03</div>' in _tiles(summary)


def test_tiles_break_a_day_tie_toward_the_earlier_day():
    summary = summarize(
        [_row(day="2026-07-09", cost=2.0), _row(day="2026-07-03", cost=2.0)],
    )
    assert '<div class="tile-value">2026-07-03</div>' in _tiles(summary)


def test_tiles_show_a_dash_rather_than_a_crash_on_an_empty_ledger():
    tiles = _tiles(summarize([]))
    assert tiles.count("—") == 2
    assert '<div class="tile-label">Projects</div><div class="tile-value">0</div>' in tiles


def test_tiles_markup_is_exact():
    # Pins all four labels, their order, the em-dash placeholders, and the fact
    # that tiles are concatenated with nothing between them.
    assert _tiles(summarize([])) == (
        '<div class="tiles">'
        '<div class="tile"><div class="tile-label">Projects</div>'
        '<div class="tile-value">0</div></div>'
        '<div class="tile"><div class="tile-label">Days with spend</div>'
        '<div class="tile-value">0</div></div>'
        '<div class="tile"><div class="tile-label">Top project</div>'
        '<div class="tile-value">—</div></div>'
        '<div class="tile"><div class="tile-label">Peak day</div>'
        '<div class="tile-value">—</div></div>'
        "</div>"
    )


# --- the table view ---------------------------------------------------------


def test_table_of_an_empty_ledger_renders_nothing():
    assert _table(summarize([])) == ""


def test_table_has_a_column_per_project_and_a_row_per_day():
    summary = summarize(
        [
            ProjectDayCost("a", "2026-07-03", 1.0),
            ProjectDayCost("b", "2026-07-03", 2.0),
            ProjectDayCost("a", "2026-07-04", 4.0),
        ]
    )
    table = _table(summary)
    assert "<th>a</th>" in table and "<th>b</th>" in table
    assert "<th>2026-07-03</th>" in table and "<th>2026-07-04</th>" in table


def test_table_shows_a_dash_where_a_project_spent_nothing_that_day():
    summary = summarize(
        [ProjectDayCost("a", "2026-07-03", 1.0), ProjectDayCost("b", "2026-07-04", 2.0)]
    )
    assert "<td>—</td>" in _table(summary)


def test_table_renders_each_cell_at_full_precision():
    assert "<td>$0.000060</td>" in _table(summarize([_row(cost=0.00006)]))


def test_table_totals_each_day_and_the_whole_ledger():
    summary = summarize(
        [ProjectDayCost("a", "2026-07-03", 1.0), ProjectDayCost("b", "2026-07-03", 2.0)]
    )
    table = _table(summary)
    assert "<td>$3.000000</td>" in table  # the day row total
    assert "<tr><th>Total</th>" in table


def test_table_footer_totals_every_project_column():
    summary = summarize(
        [ProjectDayCost("a", "2026-07-03", 1.0), ProjectDayCost("a", "2026-07-04", 2.0)]
    )
    assert "<tfoot><tr><th>Total</th><td>$3.000000</td><td>$3.000000</td></tr></tfoot>" in _table(
        summary
    )


def test_table_markup_is_exact():
    # A sparse 2x2: each project spent on one day only, so both the filled cells
    # and the em-dash gaps are pinned, along with column order (by spend, so `b`
    # leads `a`), row order (chronological), and the empty joins between cells.
    summary = summarize(
        [ProjectDayCost("a", "2026-07-03", 1.0), ProjectDayCost("b", "2026-07-04", 2.0)]
    )
    assert _table(summary) == (
        '<section class="card"><h2>Every project and day</h2>'
        '<p class="caption">The same figures the charts encode, as numbers.</p>'
        '<div class="scroll"><table>'
        "<thead><tr><th>Day</th><th>b</th><th>a</th><th>Total</th></tr></thead>"
        "<tbody>"
        "<tr><th>2026-07-03</th><td>—</td><td>$1.000000</td><td>$1.000000</td></tr>"
        "<tr><th>2026-07-04</th><td>$2.000000</td><td>—</td><td>$2.000000</td></tr>"
        "</tbody>"
        "<tfoot><tr><th>Total</th><td>$2.000000</td><td>$1.000000</td><td>$3.000000</td></tr></tfoot>"
        "</table></div></section>"
    )


# --- the empty state --------------------------------------------------------


def test_empty_note_appears_only_when_the_ledger_is_empty():
    assert "No spend recorded yet" in _empty_note(summarize([]))
    assert _empty_note(summarize([_row()])) == ""


def test_empty_note_says_an_empty_ledger_is_correct_not_broken():
    assert "not an error" in _empty_note(summarize([]))


def test_empty_note_markup_is_exact():
    assert _empty_note(summarize([])) == (
        '<section class="card"><h2>No spend recorded yet</h2>'
        '<p class="caption">The ledger counts only jobs that completed with a non-zero '
        "cost. A store holding only pending, failed, or zero-cost jobs is empty here "
        "and that is correct — it is not an error.</p></section>"
    )


# --- the whole page ---------------------------------------------------------


def test_page_is_a_standalone_html_document():
    page = render_dashboard(summarize([_row()]), _GENERATED, "/tmp/store.db")
    assert page.startswith("<!doctype html>")
    assert page.rstrip().endswith("</html>")


def test_page_pulls_in_nothing_from_the_network():
    page = render_dashboard(summarize([_row()]), _GENERATED, "/tmp/store.db")
    for remote in ("http://", "https://", "<script", "@import", "//cdn"):
        assert remote not in page


def test_page_leads_with_the_grand_total_as_the_hero_figure():
    summary = summarize([_row(cost=1.0), _row(project="b", cost=0.5)])
    page = render_dashboard(summary, _GENERATED, "/tmp/store.db")
    assert '<div class="hero-value">$1.500000</div>' in page


def test_page_stamps_when_the_data_was_read_to_the_second():
    page = render_dashboard(summarize([]), _GENERATED, "/tmp/store.db")
    assert "Generated 2026-07-23T09:30:15+00:00 from" in page


def test_page_names_the_store_it_read():
    page = render_dashboard(summarize([]), _GENERATED, "/srv/llmbus/data/llmbus.db")
    assert "<code>/srv/llmbus/data/llmbus.db</code>" in page


def test_page_carries_both_charts_when_there_is_data():
    page = render_dashboard(summarize([_row()]), _GENERATED, "/tmp/store.db")
    assert "<h2>Spend by project</h2>" in page
    assert "<h2>Spend by day</h2>" in page
    assert "<h2>Every project and day</h2>" in page


def test_page_captions_say_exactly_what_each_chart_plots():
    page = render_dashboard(summarize([_row()]), _GENERATED, "/tmp/store.db")
    assert '<p class="caption">Total USD per project, largest first.</p>' in page
    assert '<p class="caption">Total USD per day the bus recorded spend.</p>' in page


def test_page_on_an_empty_ledger_shows_the_note_and_no_charts():
    page = render_dashboard(summarize([]), _GENERATED, "/tmp/store.db")
    assert "No spend recorded yet" in page
    assert "<h2>Spend by project</h2>" not in page
    assert "<h2>Every project and day</h2>" not in page


def test_page_declares_both_light_and_dark_surfaces():
    page = render_dashboard(summarize([]), _GENERATED, "/tmp/store.db")
    assert "prefers-color-scheme: dark" in page
    assert ':root[data-theme="dark"]' in page


def test_page_uses_one_validated_hue_for_every_bar():
    # Nominal keys: shading a bar by its own size would double-encode its length.
    summary = summarize([_row(project="a", cost=1.0), _row(project="b", cost=9.0)])
    page = render_dashboard(summary, _GENERATED, "/tmp/store.db")
    assert page.count("--series: #2a78d6") == 1
    assert page.count("--series: #3987e5") == 2  # media query + data-theme scope


@pytest.mark.parametrize("width", ["width:100.0000%", "width:11.1111%"])
def test_page_bar_widths_are_proportional_to_spend(width):
    summary = summarize([_row(project="a", cost=1.0), _row(project="b", cost=9.0)])
    page = render_dashboard(summary, _GENERATED, "/tmp/store.db")
    assert width in page


def test_render_dashboard_is_deterministic_for_the_same_inputs():
    summary = summarize([_row(cost=1.0), _row(project="b", cost=2.0)])
    first = render_dashboard(summary, _GENERATED, "/tmp/store.db")
    second = render_dashboard(summary, _GENERATED, "/tmp/store.db")
    assert first == second


def test_a_summary_built_by_hand_renders_the_same_as_one_from_rows():
    rows = [_row(cost=1.0)]
    handmade = CostSummary(
        rows=(rows[0],),
        by_project=(Total("hate-moderator", Decimal(1)),),
        by_day=(Total("2026-07-03", Decimal(1)),),
        grand_total=Decimal(1),
    )
    assert render_dashboard(handmade, _GENERATED, "/x.db") == render_dashboard(
        summarize(rows), _GENERATED, "/x.db"
    )
