"""Unit tests for the model-policy page (§11, §14 #23).

Pure renderer, so no store and no clock. In the mutation gate, so assertions pin
exact markup rather than "contains something plausible".
"""

from datetime import datetime, timezone

import pytest

from llmbus.policy_page import (
    _add_form,
    _model_options,
    _policy_rows,
    _table,
    render_policy_page,
)
from llmbus.providers.base import CAPABILITIES, PROVIDERS
from llmbus.store import ModelPolicy

_AT = datetime(2026, 7, 23, 15, 0, 0, tzinfo=timezone.utc)


def _policy(project="milamber", kind="language.chat", model="gpt-5.5"):
    return ModelPolicy(project=project, kind=kind, model=model, updated_at=_AT)


# --- the model dropdown ------------------------------------------------------


def test_every_registered_model_is_offered():
    options = _model_options(None)
    for model in PROVIDERS:
        assert f'value="{model}"' in options


def test_options_are_grouped_by_capability():
    assert '<optgroup label="chat">' in _model_options(None)


def test_a_capability_nothing_serves_yet_contributes_no_group():
    # Every registered model is chat today; the page must not render empty
    # transcription/embedding groups until models exist for them.
    options = _model_options(None)
    assert '<optgroup label="transcription">' not in options
    assert '<optgroup label="embedding">' not in options


def test_a_transcription_model_gets_its_own_group(monkeypatch):
    monkeypatch.setitem(CAPABILITIES, "whisper-1", "transcription")

    options = _model_options(None)

    assert '<optgroup label="transcription"><option value="whisper-1">' in options
    # And it must NOT leak into the chat group.
    chat = options.split('<optgroup label="chat">')[1].split("</optgroup>")[0]
    assert "whisper-1" not in chat


def test_the_current_model_is_preselected():
    assert '<option value="gpt-5.4" selected>gpt-5.4</option>' in _model_options("gpt-5.4")


def test_only_the_current_model_is_preselected():
    assert _model_options("gpt-5.4").count(" selected>") == 1


def test_with_nothing_chosen_the_list_opens_on_a_placeholder():
    # Otherwise "didn't touch the dropdown" silently means "picked whichever model
    # sorts first" — on a control with ~600x of price spread behind it.
    options = _model_options(None)
    assert options.startswith('<option value="" disabled selected>choose a model…</option>')
    # Exactly one "selected" in the whole list, and it is the placeholder — no real
    # model is pre-chosen for you.
    assert options.count(" selected>") == 1
    for model in PROVIDERS:
        assert f'value="{model}" selected' not in options


def test_the_placeholder_is_absent_when_a_model_is_already_chosen():
    assert "choose a model" not in _model_options("gpt-5.4")


# --- rows and forms ----------------------------------------------------------


def test_each_row_carries_its_pair_as_hidden_fields():
    row = _policy_rows([_policy()])
    assert '<input type="hidden" name="project" value="milamber">' in row
    assert '<input type="hidden" name="kind" value="language.chat">' in row


def test_each_row_is_its_own_form():
    # One form per row, so saving one policy cannot rewrite another.
    rows = _policy_rows([_policy(kind="a"), _policy(kind="b")])
    assert rows.count('<form method="post" action="/policy">') == 2


def test_a_row_shows_when_it_last_changed():
    assert '<td class="stamp">2026-07-23T15:00:00+00:00</td>' in _policy_rows([_policy()])


def test_the_add_form_requires_every_field():
    form = _add_form()
    assert '<input name="project" placeholder="project" required>' in form
    assert 'name="kind"' in form and "required" in form
    assert '<select name="model" required>' in form


# --- the empty state ---------------------------------------------------------


def test_an_empty_policy_table_explains_the_hard_fail():
    empty = _table([])
    assert "No policies set yet" in empty
    assert "refused at submit" in empty
    assert "<table>" not in empty


def test_a_populated_table_renders_a_table():
    assert "<table>" in _table([_policy()])


# --- escaping: project and kind are producer-supplied ------------------------


def test_a_hostile_project_name_cannot_inject_markup():
    page = render_policy_page([_policy(project="<script>alert(1)</script>")], _AT)
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page


def test_a_hostile_kind_cannot_break_out_of_a_hidden_input():
    page = render_policy_page([_policy(kind='" onfocus="evil()')], _AT)
    assert 'onfocus="evil()' not in page


def test_a_notice_is_escaped_too():
    page = render_policy_page([], _AT, notice="<img src=x onerror=alert(1)>")
    assert "<img src=x" not in page
    assert "&lt;img src=x" in page


# --- the whole page ----------------------------------------------------------


def test_page_is_a_standalone_document():
    page = render_policy_page([_policy()], _AT)
    assert page.startswith("<!doctype html>")
    assert page.rstrip().endswith("</html>")


def test_page_pulls_in_nothing_from_the_network():
    page = render_policy_page([_policy()], _AT)
    for remote in ("http://", "https://", "<script", "@import", "//cdn"):
        assert remote not in page


def test_page_links_back_to_the_cost_ledger():
    assert '<a href="/">cost ledger</a>' in render_policy_page([], _AT)


def test_page_states_that_prices_are_not_editable_here():
    # The boundary from §14 #23: the UI changes model CHOICE, never price.
    assert "never editable here" in render_policy_page([], _AT)


def test_page_declares_both_light_and_dark_surfaces():
    page = render_policy_page([], _AT)
    assert "prefers-color-scheme: dark" in page
    assert ':root[data-theme="dark"]' in page


def test_page_reports_how_many_models_are_registered():
    assert f"({len(CAPABILITIES)} registered)" in render_policy_page([], _AT)


def test_render_is_deterministic_for_the_same_inputs():
    policies = [_policy()]
    assert render_policy_page(policies, _AT) == render_policy_page(policies, _AT)


@pytest.mark.parametrize("notice", [None, "", "milamber/language.chat now runs on gpt-5-nano"])
def test_the_banner_appears_only_with_a_notice(notice):
    page = render_policy_page([], _AT, notice)
    assert ('class="banner"' in page) is bool(notice)


def test_an_empty_capability_is_skipped_not_a_stopping_point(monkeypatch):
    # Only reachable when an EARLIER capability is empty and a LATER one is not:
    # order is chat, transcription, embedding, so registering an embedding model
    # while transcription stays empty means `continue` renders it and `break`
    # would silently drop it.
    monkeypatch.setitem(CAPABILITIES, "text-embedding-3-small", "embedding")

    options = _model_options(None)

    assert '<optgroup label="transcription">' not in options
    assert '<optgroup label="embedding"><option value="text-embedding-3-small">' in options


def test_model_options_markup_is_exact(monkeypatch):
    # Pins the option shape and that groups/options are concatenated with nothing
    # between them.
    monkeypatch.setattr(
        "llmbus.policy_page.models_with_capability",
        lambda capability: {"chat": ["a", "b"], "transcription": [], "embedding": []}[capability],
    )

    assert _model_options("b") == (
        '<optgroup label="chat">'
        '<option value="a">a</option>'
        '<option value="b" selected>b</option>'
        "</optgroup>"
    )


def test_policy_rows_markup_is_exact(monkeypatch):
    monkeypatch.setattr(
        "llmbus.policy_page.models_with_capability", lambda c: ["m1"] if c == "chat" else []
    )

    assert _policy_rows([_policy(project="p", kind="k", model="m1")]) == (
        "<tr><td>p</td><td>k</td>"
        '<td><form method="post" action="/policy">'
        '<input type="hidden" name="project" value="p">'
        '<input type="hidden" name="kind" value="k">'
        '<select name="model">'
        '<optgroup label="chat"><option value="m1" selected>m1</option></optgroup>'
        "</select>"
        '<button type="submit">Save</button></form></td>'
        '<td class="stamp">2026-07-23T15:00:00+00:00</td>'
        "</tr>"
    )


def test_two_rows_are_concatenated_with_nothing_between_them():
    rows = _policy_rows([_policy(kind="a"), _policy(kind="b")])
    assert "</tr><tr>" in rows


def test_empty_table_markup_is_exact():
    assert _table([]) == (
        '<p class="empty">No policies set yet. Until a pair is configured here, a job '
        "that leaves its model unset is refused at submit — deliberately, so a project "
        "never runs on a model nobody chose for it.</p>"
    )


def test_add_form_markup_is_exact(monkeypatch):
    monkeypatch.setattr(
        "llmbus.policy_page.models_with_capability", lambda c: ["m1"] if c == "chat" else []
    )

    assert _add_form() == (
        '<form method="post" action="/policy" class="add">'
        '<input name="project" placeholder="project" required>'
        '<input name="kind" placeholder="kind (e.g. language.chat)" required>'
        '<select name="model" required>'
        '<option value="" disabled selected>choose a model…</option>'
        '<optgroup label="chat"><option value="m1">m1</option></optgroup>'
        "</select>"
        '<button type="submit">Set</button></form>'
    )


def test_with_no_notice_nothing_at_all_sits_where_the_banner_would():
    # An empty string, not a placeholder: the header must run straight into the
    # first card.
    page = render_policy_page([], _AT)
    assert "</header>\n\n<section" in page


def test_populated_table_markup_is_exact(monkeypatch):
    monkeypatch.setattr(
        "llmbus.policy_page.models_with_capability", lambda c: ["m1"] if c == "chat" else []
    )
    policy = _policy(project="p", kind="k", model="m1")

    assert _table([policy]) == (
        '<div class="scroll"><table>'
        "<thead><tr><th>Project</th><th>Kind</th><th>Model</th><th>Changed</th></tr></thead>"
        f"<tbody>{_policy_rows([policy])}</tbody></table></div>"
    )
