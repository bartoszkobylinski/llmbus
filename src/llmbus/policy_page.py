"""Render the model-policy page (ARCHITECTURE.md §11, §14 #23).

The write half of the bus's web surface: which model each `(project, kind)` runs
on, and a form to change it. Pure like `dashboard.py` — rows in, HTML string out —
so it sits in the mutation gate; `server.py` owns the sockets, the auth check and
the store.

Two things it deliberately does NOT do:

- **It cannot invent a model.** Every control is a `<select>` over models already
  registered in `providers/base.py`, never a free-text box. A model the bus does
  not route would fail at `submit()` anyway (§14 #6), so offering one would only
  move the failure somewhere less obvious. It also means the page can never be
  used to set a price.
- **It cannot pick the wrong *kind* of model.** Options are grouped by capability
  (§14 #23 amendment), so once transcription models exist a chat task will not
  offer one. Today every registered model is `chat`, so there is a single group —
  the structure is here so the day `whisper-1` lands the page already separates
  them rather than needing a redesign.

`kind` is free text on the add form because it must be: it is producer-supplied
(§4) and the bus has no list of legal values. A typo therefore creates a row no
job will ever match — which fails loudly at the producer's next submit rather
than silently, and is visible here as a row nothing uses.
"""

from __future__ import annotations

import html
from collections.abc import Sequence
from datetime import datetime

from llmbus.providers.base import CAPABILITIES, Capability, models_with_capability
from llmbus.store import ModelPolicy

# Order the capability groups appear in. Explicit rather than derived from the
# registry so the page's layout does not shuffle when a model is registered.
_CAPABILITY_ORDER: tuple[Capability, ...] = ("chat", "transcription", "embedding")


def _esc(value: str) -> str:
    """HTML-escape untrusted text. `project`/`kind` are producer-supplied (§4)."""
    return html.escape(value)


def _model_options(selected: str | None) -> str:
    """Model `<option>`s grouped by capability, with `selected` pre-chosen.

    A capability nothing serves yet contributes no group at all, so the page shows
    one list today and grows sections on its own as models are registered.

    With nothing pre-selected (the add form) the list opens on a disabled
    placeholder rather than on whichever model sorts first. Otherwise filling in a
    project and kind and pressing the button without touching the dropdown would
    silently choose a model for you — on a control with roughly 600x of price
    spread behind it. The browser blocks submission while the placeholder is
    selected, and an empty model is refused server-side too (`parse_policy_form`).
    """
    groups = []
    if selected is None:
        groups.append('<option value="" disabled selected>choose a model…</option>')
    for capability in _CAPABILITY_ORDER:
        models = models_with_capability(capability)
        if not models:
            continue
        options = "".join(
            f'<option value="{_esc(model)}"{" selected" if model == selected else ""}>'
            f"{_esc(model)}</option>"
            for model in models
        )
        groups.append(f'<optgroup label="{_esc(capability)}">{options}</optgroup>')
    return "".join(groups)


def _policy_rows(policies: Sequence[ModelPolicy]) -> str:
    """One form per row: change the model for an existing `(project, kind)`.

    A form per row rather than one big form, so saving one policy cannot
    accidentally rewrite another — an operator changing `language.chat` must not
    also submit whatever was on screen for `training.analyze`.
    """
    rows = []
    for policy in policies:
        rows.append(
            f"<tr><td>{_esc(policy.project)}</td><td>{_esc(policy.kind)}</td>"
            f'<td><form method="post" action="/policy">'
            f'<input type="hidden" name="project" value="{_esc(policy.project)}">'
            f'<input type="hidden" name="kind" value="{_esc(policy.kind)}">'
            f'<select name="model">{_model_options(policy.model)}</select>'
            f'<button type="submit">Save</button></form></td>'
            f'<td class="stamp">{_esc(policy.updated_at.isoformat(timespec="seconds"))}</td>'
            f"</tr>"
        )
    return "".join(rows)


def _table(policies: Sequence[ModelPolicy]) -> str:
    if not policies:
        return (
            '<p class="empty">No policies set yet. Until a pair is configured here, a job '
            "that leaves its model unset is refused at submit — deliberately, so a project "
            "never runs on a model nobody chose for it.</p>"
        )
    return (
        '<div class="scroll"><table>'
        "<thead><tr><th>Project</th><th>Kind</th><th>Model</th><th>Changed</th></tr></thead>"
        f"<tbody>{_policy_rows(policies)}</tbody></table></div>"
    )


def _add_form() -> str:
    return (
        '<form method="post" action="/policy" class="add">'
        '<input name="project" placeholder="project" required>'
        '<input name="kind" placeholder="kind (e.g. language.chat)" required>'
        f'<select name="model" required>{_model_options(None)}</select>'
        '<button type="submit">Set</button></form>'
    )


_STYLE = """
:root { color-scheme: light; --surface:#fcfcfb; --plane:#f9f9f7; --ink:#0b0b0b;
  --ink-2:#52514e; --muted:#898781; --grid:#e1e0d9; --axis:#c3c2b7;
  --ring:rgba(11,11,11,0.10); --accent:#2a78d6; }
@media (prefers-color-scheme: dark) { :root:not([data-theme="light"]) {
  color-scheme: dark; --surface:#1a1a19; --plane:#0d0d0d; --ink:#fff;
  --ink-2:#c3c2b7; --muted:#898781; --grid:#2c2c2a; --axis:#383835;
  --ring:rgba(255,255,255,0.10); --accent:#3987e5; } }
:root[data-theme="dark"] { color-scheme: dark; --surface:#1a1a19; --plane:#0d0d0d;
  --ink:#fff; --ink-2:#c3c2b7; --muted:#898781; --grid:#2c2c2a; --axis:#383835;
  --ring:rgba(255,255,255,0.10); --accent:#3987e5; }
* { box-sizing:border-box; }
body { margin:0; padding:2rem 1.25rem 4rem; background:var(--plane); color:var(--ink);
  font-family:system-ui,-apple-system,"Segoe UI",sans-serif; line-height:1.5; }
main { max-width:60rem; margin:0 auto; }
h1 { font-size:1.125rem; font-weight:600; margin:0; }
h2 { font-size:0.9375rem; font-weight:600; margin:0 0 0.75rem; }
.meta,.caption,.empty { color:var(--muted); font-size:0.8125rem; margin:0.25rem 0 0; }
.card { background:var(--surface); border:1px solid var(--ring); border-radius:12px;
  padding:1.125rem 1.25rem 1.25rem; margin-top:1rem; }
.scroll { overflow-x:auto; }
table { border-collapse:collapse; width:100%; font-size:0.8125rem; }
th,td { text-align:left; padding:0.5rem 0.75rem; border-bottom:1px solid var(--grid);
  white-space:nowrap; }
thead th { color:var(--muted); font-weight:500; }
.stamp { color:var(--muted); font-variant-numeric:tabular-nums; }
select,input,button { font:inherit; font-size:0.8125rem; padding:0.3125rem 0.5rem;
  border:1px solid var(--axis); border-radius:6px; background:var(--surface);
  color:var(--ink); }
button { background:var(--accent); color:#fff; border-color:transparent;
  cursor:pointer; margin-left:0.375rem; }
.add { display:flex; gap:0.5rem; flex-wrap:wrap; }
.banner { border:1px solid var(--ring); border-left:3px solid var(--accent);
  background:var(--surface); border-radius:8px; padding:0.625rem 0.875rem;
  margin-top:1rem; font-size:0.8125rem; }
footer { color:var(--muted); font-size:0.75rem; margin-top:1.5rem; }
a { color:var(--accent); }
"""


def render_policy_page(
    policies: Sequence[ModelPolicy], generated_at: datetime, notice: str | None = None
) -> str:
    """The whole policy page as one standalone HTML document.

    `generated_at` is injected for the same reason as the cost page: a pure
    renderer is byte-for-byte reproducible in a test.
    """
    banner = f'<div class="banner">{_esc(notice)}</div>' if notice else ""
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>llmbus — model policy</title>
<style>{_STYLE}</style>
</head>
<body>
<main>
<header>
<h1>llmbus — model policy</h1>
<p class="meta">Which model each project and kind runs on ·
<a href="/">cost ledger</a> · {_esc(generated_at.isoformat(timespec="seconds"))}</p>
</header>
{banner}
<section class="card">
<h2>Current policy</h2>
{_table(policies)}
</section>
<section class="card">
<h2>Set a policy</h2>
<p class="caption">An existing project and kind is replaced; a new pair is added.</p>
{_add_form()}
</section>
<footer>
Models come from the bus registry ({len(CAPABILITIES)} registered) and are grouped by what
they serve. Adding a model, or changing a price, is a code change with a verified rate —
never editable here.
</footer>
</main>
</body>
</html>
"""
