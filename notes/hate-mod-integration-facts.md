# hate-moderator integration facts (survey for llmbus ↔ hate-mod)

**Produced:** 2026-07-18 by a read-only subagent survey of `~/Programming/Python/hate-moderator`.
Nothing in that repo was modified. **Every claim below is `file:line`-cited.** Anything that could
not be verified is marked `(unverified)`; absences state the grep that was run.

Context recap: llmbus ARCHITECTURE.md §14 #18 — `moderate()` stays inline in hate-mod; only
`classify()` is submitted to the bus via a callback. hate-mod stays **sync**; a `BusClient` bridge
lives in hate-mod (§14 #17).

All paths below are relative to `~/Programming/Python/hate-moderator/`.

---

## 1. Classification model (§14 #6)

- **Config default:** `app/config.py:29` — `llm_model: str = "gpt-5.4-mini"`.
- **Why this exact model** (do not silently swap it): `app/config.py:23-28` — a comment records that
  `-nano` over-fired on Polish nuance and drove the false-positive rate, and that the two cost rates
  MUST track whichever model this is.
- **Cost rates keyed to the model:** `app/config.py:30-31` —
  `llm_cost_per_million_input_tokens_usd: float = 0.75`,
  `llm_cost_per_million_output_tokens_usd: float = 4.50`.
- **`.env` actual value:** `.env` **exists** and **is gitignored** (`git check-ignore .env` → `.env`,
  ignored). It contains **no `LLM_MODEL` line** (`grep -i "^LLM_MODEL" .env` → no match; `grep -iE
  "model" .env` → no match at all). Config loads `.env` via `app/config.py:6-11`
  (`env_file=".env"`, `case_sensitive=False`).
  **=> The effective model in this repo is the config default `gpt-5.4-mini`.** No API keys or other
  secrets were read or printed.

## 2. `classify()` signature, call site, and order

- **Signature:** `app/moderation/classifier.py:146` —
  `def classify(text: str, context: str | None = None) -> tuple[Category, float, int, int]:`
- **Returns:** `(category, confidence, prompt_tokens, completion_tokens)` — `classifier.py:147`
  (docstring) and the two returns at `classifier.py:227` (success) and `classifier.py:234`
  (complete-but-unparseable fallback → `"neutral", 0.0, 0, 0`).
- **Params actually passed to the OpenAI call** (`classifier.py:165-180`):
  - `model=settings.llm_model` — `classifier.py:166`
  - `messages=[system, user]` — `classifier.py:167-170`
  - `response_format={"type": "json_object"}` — `classifier.py:171`
  - `max_completion_tokens=128` — `classifier.py:179`
  - **`temperature`: ABSENT** — not passed in the `create()` call (`classifier.py:165-180`); OpenAI
    default applies. (grep: no `temperature` in `classifier.py`.)
- **`process_comment` is SYNC:** `app/workers/poller.py:232` — `def process_comment(` (there is no
  `async def process_comment`; grep confirmed only the plain `def`).
- **`moderate()` and `classify()` call sites and order** — inside `process_comment`, the classifier
  work sits in an `if/elif/else` cascade:
  - `moderate()` is called in **two** branches:
    - allow-list branch: `poller.py:315` — `policy.decide_moderation(classifier.moderate(text))`
    - main `else` branch (the injection-immune backstop): `poller.py:370` —
      `policy.decide_moderation(classifier.moderate(text))`
  - `classify()` is called once, in the main `else` branch: `poller.py:427-429` —
    `classifier.classify(text, context=context)`.
  - **Order in the main path: `moderate()` FIRST (poller.py:370) → daily-cap gate
    (poller.py:395-415) → `classify()` (poller.py:427).** `moderate()` runs *before* `classify()`
    and short-circuits it on a hit (`poller.py:376-386`). This matches §14 #18: `moderate()` is the
    cheap inline backstop that stays; only the paid `classify()` call is a bus candidate.
  - Latency is timed around `classify()` only: `started = time.monotonic()` at `poller.py:426`,
    `latency_ms = int(... * 1000)` at `poller.py:430`.

## 3. Callback receiver surface / internal auth (B4)

- **`/internal/*` routes: ABSENT.** `grep -rn "internal" app/ --include="*.py"` → **no matches**
  (exit 1). There is no internal route namespace and no internal shared-secret/HMAC auth today.
- **Existing auth patterns a bus→hate-mod callback could reuse:**
  - **Meta webhook HMAC (the strongest reusable pattern):** `app/routes/webhook.py:100-106`
    `_verify_signature(body, signature_header, app_secret)` — computes
    `hmac.new(app_secret.encode(), body, hashlib.sha256).hexdigest()` (`webhook.py:105`) and compares
    with `hmac.compare_digest(f"sha256={expected}", signature_header)` (`webhook.py:106`,
    constant-time). Header read at `webhook.py:42` (`X-Hub-Signature-256`), enforced at
    `webhook.py:43-45` (403 on mismatch). A bus callback should mirror this: HMAC-SHA256 over the raw
    body with a **new** shared secret, `hmac.compare_digest`.
  - **Verify-token compare (bootstrap/GET):** `webhook.py:28` —
    `hmac.compare_digest(hub_verify_token, settings.meta_webhook_verify_token)`.
  - **Signed-token serializer (for opaque, expiring tokens):** `app/security/tokens.py:15` uses
    `itsdangerous.URLSafeTimedSerializer` (`BadSignature`/`SignatureExpired` handling at
    `tokens.py:40,51,60`). Could back a signed callback token if HMAC-over-body is not preferred.
  - No shared secret for a bus callback exists in config yet — a new `.env`/`config.py` setting would
    be needed (config pattern: `app/config.py:5-65`, all secrets are `BaseSettings` fields).

## 4. State plumbing at classify time (B5)

**Locals in scope at the `classify()` call (`poller.py:427-429`)** that a callback would need
threaded back to finish the comment:

| Value | Where bound |
| --- | --- |
| `comment_id` | `poller.py:241` (`str(comment["id"])`) |
| `text` | `poller.py:242` (`str(comment.get("text",""))`) |
| `commenter_username` | `poller.py:243` |
| `media_id` | function param, `poller.py:235` |
| `text_hash` | `poller.py:275` (`_text_hash(text)`) |
| `user` / `user.id` | function param, `poller.py:234`; `user.id` used at 528+ |
| `context` | `poller.py:420-424` (queried from `Media.context_summary`) |
| `mode` | `poller.py:277` |
| `threshold_bonuses` / `bonus` | param `poller.py:237`; `bonus` computed `poller.py:459-462` |
| `latency_ms` | declared `poller.py:281`, set `poller.py:430` |
| `prompt_tokens`, `completion_tokens` | returned by `classify()`, consumed `poller.py:539-547` |

- **Canonical "state to thread back" set already exists** as the retry-queue row.
  `_enqueue_pending(...)` signature (`poller.py:162-171`) persists exactly:
  `comment_id, media_id, text, commenter_username, reason`. The `PendingComment` model
  (`app/models.py:283`+) stores: `user_id` (`models.py:26`), `ig_comment_id` (`models.py:27`),
  `ig_media_id` (`models.py:28`), `text` (`models.py:29`), `commenter_username` (`models.py:30`),
  `reason` (`models.py:33`), `attempts` (`models.py:38`), `claimed_at` (`models.py:50`).
  **Note:** `text_hash` and `context` are **not** persisted — on a retry they are recomputed
  (`text_hash` at `poller.py:275`) / re-queried (`context` at `poller.py:420-424`). A bus callback
  would likewise need to either carry them or recompute them.

- **The single `db.commit()` a two-process split would break:** `app/workers/poller.py:568`.
  This is the terminal commit that atomically bundles, in one unit (comment at `poller.py:522-527`):
  - the dedup token: `ClassifiedComment` insert (`poller.py:528-542`),
  - the LLM cost counters on `user` (`poller.py:543-548`),
  - the `HiddenComment` row when hidden (`poller.py:549-560`),
  - deletion of the `PendingComment` retry row (`poller.py:564-566`).
  The unique constraints on `ClassifiedComment.ig_comment_id` / `HiddenComment.ig_comment_id` are the
  race gate (`poller.py:523-527`); `IntegrityError` rolls the whole unit back (`poller.py:569-574`).
  **Critical ordering:** the Meta hide (`ig_client.hide_comment`, `poller.py:480`) happens *inline,
  before* this commit (`poller.py:478-520`). So the decision→hide→commit tail is a single synchronous
  transaction today. Splitting `classify()` to the bus means only the *verdict* returns async; the
  callback path must then re-enter the hide + commit tail (poller.py:465-574) itself.
  (Other, earlier commits exist and are NOT the one meant here: enqueue commit `poller.py:222`,
  claim-lease commit `poller.py:617`, webhook liveness stamp `poller.py:941`, auth-error commits.)

## 5. BusClient lifecycle hook points (§14 #17 open detail)

- **Primary hook — FastAPI lifespan (recommended):** `app/main.py:220-235`
  `@asynccontextmanager async def lifespan(app)`, registered at `app/main.py:265`
  (`app = FastAPI(title="hate-moderator", lifespan=lifespan)`). It is already async and already the
  place startup work runs (`configure_logging`, migrations, sweeps at `main.py:225-234`). A persistent
  asyncio loop + one long-lived `BusClient` would be created here and torn down after `yield`
  (`main.py:235`).
- **Why `run_coroutine_threadsafe` is needed:** `process_comment` is sync (`poller.py:232`) and is
  invoked off the request thread — webhooks schedule it via
  `background_tasks.add_task(poller.handle_webhook_comment, ...)` (`app/routes/webhook.py:88-95`), and
  the immediate backfill runs it on a raw `threading.Thread` (`poller.py:150`). So a sync caller
  reaching an async `BusClient` bound to the lifespan loop must hop threads via
  `run_coroutine_threadsafe(coro, loop)` against the loop captured at lifespan startup. Deploy is a
  **single uvicorn worker** (documented at `poller.py:136-138`), so one loop + one client is sound.
- **SURPRISE / second entrypoint — the cron drain has NO lifespan and NO event loop:**
  `app/scripts/drain_queue.py` is a separate process. Its entrypoint is a plain sync `def main()`
  (`drain_queue.py:116-120`) → `configure_logging` + `drain_all()`; `drain_all()`
  (`drain_queue.py:58-113`) calls `poller.drain_user_queue` → `drain_pending` → `process_comment` →
  `classify()`. **This process never starts the FastAPI app, so it gets no lifespan loop and no
  BusClient.** If `classify()` becomes a bus submit, the hourly cron path (`drain_queue.py`) either
  needs its own `BusClient`/`asyncio.run` per invocation, or must be reworked so the cron only
  re-queues and never itself calls the bus. This is the one fact that most changes the integration
  plan — the bus wiring is not "one place in `main.py`"; there are **two** processes that reach
  `classify()`.

## 6. `response_format` today (B1) — what json_schema replaces

- **`response_format={"type": "json_object"}`:** `app/moderation/classifier.py:171`.
- **Fail-open "unparseable → neutral":** `classifier.py:228-234` — a **complete** (non-truncated,
  non-empty) response that still won't `json.loads` is caught and returns `"neutral", 0.0, 0, 0`
  (`classifier.py:234`). Also `classifier.py:210-213`: an out-of-taxonomy `category` value is coerced
  to `"neutral"`.
- **Nuance — the fail-open is narrow, not "any bad response":** a **truncated**
  (`finish_reason == "length"`) or **empty** response does **not** fail open. It raises
  `ClassifierIncompleteError` (`classifier.py:192-207`, class at `classifier.py:16-25`), which the
  caller treats as retryable and re-queues (`poller.py:431-451`) — i.e. fail **closed**. A service
  error (timeout/5xx) likewise re-raises (`classifier.py:181-188`). So only a *complete-but-garbage*
  JSON body falls back to neutral. When swapping to `json_schema`, preserve this split: schema
  violations on a complete response should still fail open to neutral; truncation/emptiness should
  still fail closed to retry.
