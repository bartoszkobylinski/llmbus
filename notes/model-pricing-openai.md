# OpenAI model lineup & pricing — research for the llmbus cost ledger

**Research date: 2026-07-20** (task framed as "today is 2026-07-19"; all fetches below were
made on 2026-07-20 local time).
**Purpose:** verify `src/llmbus/cost.py::PRICING` against currently published OpenAI prices.

Every price in this document carries a source URL and the date it was retrieved. Anything I
could not source is written `(unverified)` and repeated in the
[What I could NOT verify](#what-i-could-not-verify) section at the bottom. Nothing here is
interpolated from older prices or inferred from a sibling model.

Primary source used throughout: the official OpenAI developer docs at
`developers.openai.com`. The marketing/news host `openai.com` returned **HTTP 403** to every
fetch attempt (see below), so no price in this file depends on it.

---

## 1. Does `gpt-5.4-mini` exist?

**YES.** It is a real model with the exact API id **`gpt-5.4-mini`**.

| Field | Value | Source | Retrieved |
|---|---|---|---|
| Exact API model id | `gpt-5.4-mini` | <https://developers.openai.com/api/docs/models/gpt-5.4-mini> | 2026-07-20 |
| Positioning (official) | "Our strongest mini model yet for coding, computer use, and subagents" | same | 2026-07-20 |
| Context window | 400,000 tokens; 128,000 max output | same | 2026-07-20 |
| Input | **$0.75 / 1M tokens** | <https://developers.openai.com/api/docs/pricing> and the model page above (both agree) | 2026-07-20 |
| Cached input | **$0.075 / 1M tokens** | same two pages | 2026-07-20 |
| Output | **$4.50 / 1M tokens** | same two pages | 2026-07-20 |
| Structured outputs (`json_schema`) | Supported | model page | 2026-07-20 |
| Function calling | Supported | model page | 2026-07-20 |
| Reasoning effort levels | `none` (listed as default), `low`, `medium`, `high`, `xhigh` | model page | 2026-07-20 |

**When it appeared:** widely reported as **2026-03-17**, alongside `gpt-5.4-nano`, with the
base `gpt-5.4` having landed ~2026-03-05.

- Official announcement URL exists — <https://openai.com/index/introducing-gpt-5-4-mini-and-nano/>
  — but **WebFetch got HTTP 403** on it (2026-07-20), so I could not read it directly. The date
  comes from search-result snippets of that page plus third-party coverage:
  - 9to5Mac, "OpenAI releases GPT-5.4 mini and nano, its 'most capable small models yet'",
    <https://9to5mac.com/2026/03/17/openai-releases-gpt-5-4-mini-and-nano-its-most-capable-small-models-yet/>
    (**third-party press**, retrieved 2026-07-20)
  - GitHub Changelog, "GPT-5.4 mini is now generally available for GitHub Copilot", 2026-03-17,
    <https://github.blog/changelog/2026-03-17-gpt-5-4-mini-is-now-generally-available-for-github-copilot/>
    (**third-party**, and note this is *Copilot* availability, not the OpenAI API GA date)
- Caution on the docs pages: the model detail page renders a date of **2025-08-31** for the
  whole GPT-5.4 family, which is the **knowledge cutoff**, not the release date. Do not read
  it as a launch date.

So: the release *date* is **third-party-sourced only**. The model's *existence*, *id*, and
*prices* are verified against official docs.

> Relevance to this repo: `ARCHITECTURE.md` §14 #6 (2026-07-18) already pins hate-mod's prod
> classifier to `gpt-5.4-mini`. That decision names a model that genuinely exists. See §4 for
> the problem this creates in `cost.py`.

---

## 2. Current OpenAI chat-completion lineup a production classifier would use

All rows below are USD per 1,000,000 tokens.
Source for the whole table unless noted: **<https://developers.openai.com/api/docs/pricing>,
retrieved 2026-07-20**. Rows marked † were additionally cross-checked against that model's own
docs page (`developers.openai.com/api/docs/models/<id>`) on the same date and matched exactly.

### Current families (newest first)

| API model id | Input | Cached input | Output | Notes |
|---|---|---|---|---|
| `gpt-5.6-sol` † | 5.00 | 0.50 | 30.00 | Frontier tier of the GPT-5.6 family |
| `gpt-5.6-terra` † | 2.50 | 0.25 | 15.00 | "Balances intelligence and cost" |
| `gpt-5.6-luna` † | 1.00 | 0.10 | 6.00 | Cheapest 5.6 tier — but see the cost trap below |
| `gpt-5.5` † | 5.00 | 0.50 | 30.00 | |
| `gpt-5.5-pro` | 30.00 | — (none listed) | 180.00 | |
| `gpt-5.4` † | 2.50 | 0.25 | 15.00 | |
| **`gpt-5.4-mini`** † | **0.75** | **0.075** | **4.50** | hate-mod's chosen prod classifier |
| **`gpt-5.4-nano`** † | **0.20** | **0.02** | **1.25** | "Our cheapest GPT-5.4-class model for simple high-volume tasks… classification, data extraction, ranking, and sub-agent applications" |
| `gpt-5.4-pro` | 30.00 | — (none listed) | 180.00 | |
| `gpt-5.3-codex` | 1.75 | 0.175 | 14.00 | Coding-specialised |
| `chat-latest` | 5.00 | 0.50 | 30.00 | Moving alias — **never** put an alias in a price table; it silently repoints |

`gpt-5.6` is documented as an **alias for `gpt-5.6-sol`**
(<https://developers.openai.com/api/docs/models>, retrieved 2026-07-20). Same warning as
`chat-latest`: aliases must not be priced.

The GPT-5.6 family (Sol / Terra / Luna) has a 1,050,000-token context window, 128,000 max
output, and a February 2026 knowledge cutoff (model pages, retrieved 2026-07-20). It was
released publicly on **2026-07-09** after a limited preview from 2026-06-26 — this date is
from **third-party coverage** (Simon Willison, <https://simonwillison.net/2026/Jul/9/gpt-5-6/>;
VentureBeat; MarkTechPost; all retrieved 2026-07-20), because the official
`openai.com/index/previewing-gpt-5-6-sol/` page was not fetched (the openai.com host 403s).

### GPT-5 family (the original — still live)

| API model id | Input | Cached input | Output | Source | Retrieved |
|---|---|---|---|---|---|
| `gpt-5` | 1.25 | 0.125 | 10.00 | <https://developers.openai.com/api/docs/models/gpt-5> | 2026-07-20 |
| `gpt-5-mini` | 0.25 | 0.025 | 2.00 | <https://developers.openai.com/api/docs/models/gpt-5-mini> | 2026-07-20 |
| `gpt-5-nano` | 0.05 | 0.005 | 0.40 | <https://developers.openai.com/api/docs/models/gpt-5-nano> | 2026-07-20 |

**Important sourcing caveat:** these three did **not appear** in the pricing-page table I
retrieved — that page showed only the 5.3–5.6 era models. Their prices above come from each
model's own official docs page. Whether they sit in a "legacy/older models" section further
down the pricing page that my fetch did not surface, I **could not confirm**.

Status per their docs pages (retrieved 2026-07-20):
- None of the three is marked deprecated or legacy; all remain active.
- `gpt-5` is described as the "previous intelligent reasoning model", with docs recommending
  an upgrade to GPT-5.6.
- `gpt-5-nano`'s page recommends starting new projects on **GPT-5.6 Luna**.
- **No retirement date is published for any of them** — `(unverified)`, I found none.

### Cost trap worth flagging before anyone "upgrades"

The docs recommend `gpt-5.6-luna` as the successor to `gpt-5-nano`, but Luna is **20× the
input price and 15× the output price** of `gpt-5-nano` ($1.00/$6.00 vs $0.05/$0.40), and
**5× / 4.8×** the price of `gpt-5.4-nano` ($0.20/$1.25). "Cheapest tier of the newest family"
is not "cheap". For a high-volume classifier, the cheap options today are `gpt-5.4-nano`
(0.20 / 1.25), then `gpt-5.4-mini` (0.75 / 4.50) — not anything in the 5.6 line.

---

## 3. Is llmbus's price table stale?

`src/llmbus/cost.py` currently holds (verified by reading the file, lines 57–59, on 2026-07-20):

```python
"gpt-5":      1.25 in / 10.00 out
"gpt-5-mini": 0.25 in /  2.00 out
"gpt-5-nano": 0.05 in /  0.40 out
```

Compared against the official model pages retrieved 2026-07-20:

| Model | llmbus input | Published input | llmbus output | Published output | Verdict |
|---|---|---|---|---|---|
| `gpt-5` | 1.25 | **1.25** | 10.00 | **10.00** | ✅ correct, no drift |
| `gpt-5-mini` | 0.25 | **0.25** | 2.00 | **2.00** | ✅ correct, no drift |
| `gpt-5-nano` | 0.05 | **0.05** | 0.40 | **0.40** | ✅ correct, no drift |

**All three prices are still accurate. Zero differences. Nothing to correct.**

The `cost.py` docstring claims the rates were "verified 2026-07-03"; as of 2026-07-20 that
verification still holds for these three entries.

### The actual staleness is a MISSING row, not a wrong one

`PRICING` contains **no entry for `gpt-5.4-mini`** — nor for any 5.4/5.5/5.6-era model. But
`ARCHITECTURE.md` §14 #6 (decided 2026-07-18) states hate-mod's prod classifier **is**
`gpt-5.4-mini`, and asserts that "stawki kosztu są przypięte do tego modelu" (cost rates are
pinned to this model). In this repo they are not.

Consequence, read straight off `cost.py::price_for`:

```python
points = PRICING.get(model)
if points is None:
    raise UnknownModelError(model)
```

Every hate-mod job routed to `gpt-5.4-mini` will raise **`UnknownModelError`** at costing
time. The module's own design intent — "raised rather than under-counting cost" — means this
fails loud rather than mis-bills, which is the correct behaviour, but the ledger cannot price
the production model at all until a row is added.

Suggested addition (prices verified 2026-07-20, sources in §1/§2 above). Note that
`cost.py`'s `_EPOCH` is `date(2025, 1, 1)`, which for `gpt-5.4-*` predates the model's own
existence; a `PricePoint` effective **2026-03-17** would be more honest, but that date is
third-party-sourced, so flag it in the comment rather than presenting it as verified:

```python
# OpenAI — GPT-5.4 family. Rates verified 2026-07-20 against
# developers.openai.com/api/docs/pricing and .../models/gpt-5.4-mini.
# Effective date = _EPOCH because the model's real launch date (reported 2026-03-17)
# is third-party-sourced only; no job predates it in practice.
"gpt-5.4-mini": (PricePoint(_EPOCH, ModelPricing(Decimal("0.75"), Decimal("4.50"))),),
"gpt-5.4-nano": (PricePoint(_EPOCH, ModelPricing(Decimal("0.20"), Decimal("1.25"))),),
```

Two further gaps the ledger does not model at all (both pre-existing, both out of scope for a
price-refresh but worth recording):

1. **Cached input is not represented.** `ModelPricing` has only `input_per_mtok` /
   `output_per_mtok`. Cached input is 10% of input across every model I checked. Any workload
   with a stable system prompt will be **over-billed** by the ledger.
2. **Aliases must stay out of `PRICING`.** `chat-latest` and `gpt-5.6` repoint over time; a
   pinned price against a moving alias is a silent falsehood the moment OpenAI moves it.

---

## 4. Structured output (`json_schema` + `strict`) and reasoning-token budgeting

### (a) Structured output

Source: <https://developers.openai.com/api/docs/guides/structured-outputs>, retrieved 2026-07-20.

- Configured on Chat Completions via
  `response_format: { "type": "json_schema", "json_schema": {...}, "strict": true }` — the
  same shape llmbus already maps in the OpenAI adapter (§14 #10).
- Availability: "Structured Outputs is available in our latest large language models, starting
  with GPT-4o. For new projects, start with `gpt-5.6`." Older models (e.g. `gpt-4-turbo`) get
  JSON mode instead.
- **`gpt-5.4-nano` explicitly documents support for both `json_schema` and `strict`**
  (<https://developers.openai.com/api/docs/models/gpt-5.4-nano>, retrieved 2026-07-20).
  **`gpt-5.4-mini` documents "Structured outputs / JSON schema: supported"**
  (its model page, same date) — its page does not spell out the word `strict` separately, so
  strict-mode support for `-mini` specifically is `(inferred from the family, not quoted)`.
  This mirrors the gap ARCHITECTURE.md §14 #6 already flags: the live verification ran on
  `gpt-5-nano`, never on `-mini`. A one-shot live check against `gpt-5.4-mini` would close it.
- Schema subset constraints confirmed: required fields must be declared explicitly, and
  `additionalProperties: false` is required — exactly what llmbus's `schema.py` validator
  already enforces (§14 #10).
- **First request with a new schema carries extra latency** while the API compiles it;
  subsequent requests with the same schema do not. Relevant to any p99 latency budget and to
  a cold-start smoke test.
- The guide explicitly tells callers to handle responses that fail to match the schema due to
  **refusals** (programmatically detectable) or **incomplete generation from hitting token
  limits**. This is precisely the failure llmbus patched in §14 #10 — the adapter now returns
  a completion only on a clean `finish_reason == "stop"`. That patch is aligned with official
  guidance, not merely a local workaround.

### (b) Reasoning-token budgeting — directly relevant to the 128-token blowout

Source: <https://developers.openai.com/api/docs/guides/reasoning>, retrieved 2026-07-20.

llmbus's finding (ARCHITECTURE.md §14 #10, live test 2026-07-17): `gpt-5-nano` spent 448
reasoning tokens on a one-line prompt, so a `max_completion_tokens=128` budget terminated with
`finish_reason="length"` and an **empty** completion. The official docs confirm this is
expected behaviour, not a fluke:

> "This might occur before any visible output tokens are produced, meaning you could incur
> costs for input and reasoning tokens without receiving a visible response."

Concrete guidance from that page:

- **Reserve at least 25,000 tokens** for reasoning + output when starting out with reasoning
  models, tightening the buffer once you have measured your own prompts. llmbus's current
  working rule of "~1–2k tokens minimum for structured output on GPT-5" (§14 #10) is far
  below OpenAI's starting recommendation — empirically derived and probably fine for a
  one-line classifier verdict, but it should be recorded as *measured for our prompt*, not as
  a general floor.
- Reasoning effort settings documented: **`none`, `minimal`, `low`, `medium`, `high`,
  `xhigh`, `max`**. `none` is described as best for "Latency-critical tasks that do not
  benefit from any reasoning or multi-chained tool calls" — i.e. it effectively disables
  reasoning.

**The important delta for llmbus:** the old `gpt-5` family page lists effort levels
`minimal / low / medium / high` — no `none`. Both `gpt-5.4-mini` and `gpt-5.4-nano` list
**`none` (shown as the default), low, medium, high, xhigh** (their model pages, retrieved
2026-07-20). If that default holds, migrating hate-mod's classifier to `gpt-5.4-mini` largely
dissolves the reasoning-budget trap: a short structured-output verdict would not be preceded
by hundreds of unbilled-to-the-user-but-billed-to-us reasoning tokens.

Do **not** ship on that assumption without a live check. The "(default)" marking was read from
a summarised render of the docs page, and defaults are exactly the kind of detail that page
summaries get wrong. Verify by issuing one real call to `gpt-5.4-mini` and reading
`usage.completion_tokens_details.reasoning_tokens` — llmbus already has the harness for this
in `tests/integration/test_live_api.py`. Keep the fail-loud `finish_reason` guard regardless;
it is correct independently of effort defaults.

---

## What I could NOT verify

Listed plainly. None of these gaps was filled with a plausible guess.

1. **`openai.com` is unreachable to my fetcher.** Both
   <https://openai.com/index/introducing-gpt-5-4-mini-and-nano/> and
   <https://openai.com/api/pricing/> returned **HTTP 403 Forbidden** (2026-07-20). No price in
   this document depends on either page — everything priced came from `developers.openai.com`.
2. **The release date of `gpt-5.4-mini` / `gpt-5.4-nano` (reported 2026-03-17) is
   third-party-sourced only** (9to5Mac, GitHub Changelog, search snippets of the official
   announcement). I did not read it on an OpenAI-official page.
3. **The GPT-5.6 GA date (reported 2026-07-09) is likewise third-party-sourced only**
   (Simon Willison's blog, VentureBeat, MarkTechPost — all clearly non-official).
4. **No "last updated" marker was found on the pricing page**, so I cannot state how fresh
   OpenAI's own table is — only when *I* read it (2026-07-20).
5. **`gpt-5`, `gpt-5-mini`, `gpt-5-nano` were absent from the pricing-page table I
   retrieved.** Their prices come from individual model pages. Whether a legacy section of the
   pricing page also lists them (and agrees) is unconfirmed.
6. **No deprecation or retirement dates found** for `gpt-5` / `gpt-5-mini` / `gpt-5-nano`.
   Their pages say "not deprecated"; that is the absence of a date, not a commitment.
7. **GPT-5.1 / 5.2 / 5.3 (non-codex) variants**: `gpt-5.4`'s page references GPT-5.2 in
   passing, and `gpt-5.3-codex` is priced, but I did not enumerate or price the 5.1/5.2/5.3
   base models. `(unverified)` — assume nothing about them.
8. **Batch API discounts, priority/flex processing tiers, and any long-context surcharge**
   were not investigated. If the ledger ever prices batch traffic at the standard rate it will
   over-bill. `(unverified)`
9. **Cached-input semantics** (what qualifies, minimum prefix length, TTL) were not researched
   — only the headline per-million rates.
10. **`strict: true` on `gpt-5.4-mini` specifically** is not quoted verbatim on its model page;
    only "structured outputs / JSON schema: supported" is. Family-level inference, flagged.
11. **`none` as the *default* reasoning effort for `gpt-5.4-mini`/`-nano`** comes from a
    summarised render of the docs pages. Treat as strong indication, not fact, until a live
    call confirms it.
12. **`gpt-5.5-pro` / `gpt-5.4-pro` cached-input rates** are shown as "—" on the pricing page.
    Whether that means "caching unsupported" or "not published" is unconfirmed.
