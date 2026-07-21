# hate-moderator integration facts — REFRESHED 2026-07-21

**Provenance:** produced by a read-only `Explore` subagent on 2026-07-21. The subagent had no
write tools, so it returned the content inline and this file was **transcribed verbatim by the
parent session** (only change: HTML entity escapes from the tool channel restored to `->` / `<`).
The parent has **not** independently re-verified these citations — they carry the subagent's
authority, not a second reading. Counts as returned: **47 verified claims, 5 changed-since findings.**

Supersedes `notes/hate-mod-integration-facts.md` (2026-07-18) — see §0 for what that note got wrong.

**Verified at HEAD `b906a38`** (`git rev-parse --abbrev-ref HEAD` → `main`; working tree has no
modified files under `app/`, only untracked `analysis/` and `docs/*` artifacts). Paths relative to
`~/Programming/Python/hate-moderator/`. Nothing modified.

## 0. CHANGED SINCE 2026-07-18

**The headline delta is that there is no code delta.** The premise of this survey task does not hold:

1. **The three "new" lease commits predate the old note.** `git log --format="%h AUTHOR:%ad COMMIT:%cd"`
   → `30d2940` 2026-07-14 15:02:50, `376699b` 2026-07-14 15:09:47, `b906a38` 2026-07-14 22:34:05
   (author and committer dates identical, no rebase skew). The old note is dated 2026-07-18.
   **`b906a38` is HEAD and was already HEAD when the old note was written** — the note already cites
   the lease (`claim-lease commit poller.py:617`, old note line 124, and `claimed_at`, old note line
   106). The lease rework was *not* landed "since then."
2. Consequently **every poller.py / classifier.py / main.py / config.py / drain_queue.py line number
   in the old note re-verified as still correct** at HEAD. They were not stale.
3. **Stale citation found (the one real correction):** old note lines 104-106 cite `PendingComment`
   fields as `user_id (models.py:26)`, `ig_comment_id (models.py:27)`, `ig_media_id (models.py:28)`,
   `text (models.py:29)`, `commenter_username (models.py:30)`, `reason (models.py:33)`,
   `attempts (models.py:38)`, `claimed_at (models.py:50)`. **All eight are wrong.** Actual:
   `user_id` `models.py:308`, `ig_comment_id` `models.py:309`, `ig_media_id` `models.py:310`,
   `text` `models.py:311`, `commenter_username` `models.py:312`, `reason` `models.py:315`,
   `attempts` `models.py:320`, `claimed_at` `models.py:332`. (The old note's own `app/models.py:283`
   for the class itself is correct — `models.py:283`.)
4. **Old note omission:** it lists `last_attempt_at` nowhere, though the lease design turns on
   `claimed_at` being *distinct* from it (`models.py:322` vs `models.py:332`; rationale
   `models.py:330-331`).
5. **Old note omission (material for llmbus):** §5 does not state that the lease is taken **only on
   the drain path**, never on the live webhook path. See §4c below — this is the load-bearing fact
   for bus re-drive.

## 1. `classify()` — signature, return, OpenAI params

- Signature: `app/moderation/classifier.py:146` —
  `def classify(text: str, context: str | None = None) -> tuple[Category, float, int, int]:`
- Return contract `(category, confidence, prompt_tokens, completion_tokens)`: docstring
  `classifier.py:147`; success return `classifier.py:227`; fallback return `classifier.py:234`
  (`return "neutral", 0.0, 0, 0`).
- The `create()` call spans `classifier.py:165-180`:
  - `model=settings.llm_model` — `classifier.py:166`
  - `messages=[system, user]` — `classifier.py:167-170`
  - **`response_format={"type": "json_object"}` — `classifier.py:171`. Line UNCHANGED, still exactly 171.**
  - `max_completion_tokens=128` — `classifier.py:179`
  - **`temperature`: ABSENT.** `grep -n "temperature" app/moderation/classifier.py` → exit 1, no matches.
- Prompt is nonce-fenced before the call: `classifier.py:155-156`; context path swaps the system
  prompt `classifier.py:159-163`.

## 2. `process_comment` — sync, and the ordering

- **Still sync `def`:** `app/workers/poller.py:232` — `def process_comment(`.
  `grep -rn "process_comment" app/ --include="*.py"` returns no `async def` form anywhere.
- Ordering in the main `else` branch, all confirmed at current lines:
  - `moderate()` backstop — `poller.py:370` (`policy.decide_moderation(classifier.moderate(text))`);
    short-circuits on hit at `poller.py:376-386`.
  - daily-cap gate — `poller.py:395-398`, defer + `_enqueue_pending(reason="cap_deferred")`
    `poller.py:406-415`.
  - `classify()` — `poller.py:427-429`, timed by `started` `poller.py:426` and `latency_ms` `poller.py:430`.
  - Failure path (outage or `ClassifierIncompleteError`) → `_enqueue_pending(reason="classifier_unavailable")`
    `poller.py:442-451`.
- Second `moderate()` call site, allow-list branch: `poller.py:315`.
- **Order holds: `moderate()` (370) → cap (395) → `classify()` (427).**

## 3. The terminal `db.commit()`

- **`app/workers/poller.py:568`** — unchanged line. What it bundles (also unchanged by the lease work):
  - `ClassifiedComment` insert — `poller.py:528-542` (with
    `billed=bool(prompt_tokens or completion_tokens)` `poller.py:539`, `latency_ms` `poller.py:540`)
  - cost counters on `user` — `poller.py:543-548`
  - `HiddenComment` when hidden — `poller.py:549-560`
  - `PendingComment` deletion — `poller.py:564-566`
- `IntegrityError` rollback — `poller.py:569-574`; race-gate rationale comment `poller.py:522-527`.
- Meta hide happens **inline before** this commit — `ig_client.hide_comment` `poller.py:480`, whole
  hide block `poller.py:478-520`.
- **The lease rework did NOT change what this commit bundles.** It adds no `claimed_at` write here;
  the lease is released implicitly because the row is deleted at `poller.py:564-566`. Other commits
  (not this one): enqueue `poller.py:222`, lease claim `poller.py:617`, stale-row drop `poller.py:662`,
  webhook liveness `poller.py:941`.

## 4. THE LEASE REWORK (30d2940 / 376699b / b906a38)

### 4a. What it does now

- Constant: `CLAIM_LEASE = timedelta(minutes=5)` — `poller.py:582`; rationale `poller.py:577-581`
  (must outlast any real `classify()` "by orders of magnitude"; a crashed worker costs at most one
  cron cycle).
- `_claim_pending(db, row) -> bool` — `poller.py:585`. Implementation `poller.py:606-618`:
  `now = datetime.now(UTC)` (`606`); claimable predicate
  `or_(claimed_at IS NULL, claimed_at < now - CLAIM_LEASE)` (`607-610`); atomic
  `UPDATE ... .update({"claimed_at": now}, synchronize_session=False)` (`611-615`); `db.commit()`
  (`617`); returns `bool(claimed)` i.e. rowcount (`618`).
- Column: `claimed_at` `models.py:332`, semantics comment `models.py:323-331`. Boot migration:
  `("pending_comments", "claimed_at", "DATETIME")` — `app/main.py:86`, applied by
  `_migrate_add_columns()` `main.py:34`, called from lifespan `main.py:231`.
- Why a lease and not a version check: `poller.py:599-604` and commit message of `376699b` — a worker
  arriving *while another is inside `classify()`* reads the row happily and re-claims it; only a
  still-warm claim turns it away.
- Release on failure: `_enqueue_pending` sets `row.claimed_at = None` — `poller.py:220`, rationale
  `poller.py:216-219` (leaving it claimed would convert the retry-on-next-tick contract into a
  5-minute backoff). Note this is in the **update** branch only (`poller.py:200-220`); the insert
  branch `poller.py:187-199` never sets `claimed_at`, so a new row defaults NULL = claimable.
- Release on success: implicit, via row deletion at `poller.py:564-566`.
- Call site: **exactly one** — `poller.py:664`, inside `drain_pending`.
  `grep -rn "_claim_pending" app/ --include="*.py"` → `poller.py:585` (def), `poller.py:664` (call),
  plus two docstring mentions (`poller.py:591`, `drain_queue.py:22`). Loser branch logs and
  `continue`s — `poller.py:664-673`.
- Tests pinning it: `tests/test_poller.py:560` (winner/loser + reclaim after expiry, expiry forced at
  `:608`), `tests/test_poller.py:716` (crashed-worker row stays re-claimable, `:747`),
  `tests/test_poller.py:787` (skip within lease, retry after expiry, `:834`;
  `hide.assert_called_once_with` `:848`).

### 4b. Expiry semantics — precisely

A row is claimable iff `claimed_at IS NULL` **or** `claimed_at < now - 5min` (`poller.py:607-610`).
There is **no heartbeat, no renewal, no explicit release call** — `grep` finds no writer of
`claimed_at` other than `poller.py:615` (claim) and `poller.py:220` (clear on requeue). So the lease
is a fixed 5-minute wall-clock window from the instant of claim, and it is never extended.

### 4c. Interaction with an async bus round-trip — the critical part

**Yes, a lease will expire while a bus job is in flight, and the design cannot prevent it as written.**

- The lease was sized against a *synchronous* OpenAI call bounded by `timeout=15.0, max_retries=1`
  (`classifier.py:142`) — hence the "orders of magnitude" claim at `poller.py:580`. A bus round-trip
  (submit → queue wait → worker → callback) has no such bound. Any job whose end-to-end latency
  exceeds `CLAIM_LEASE` (`poller.py:582`, 5 min) leaves the row claimable again, and the next drain
  tick re-claims and **re-submits** it — a second billed classification and a second `hide`, which is
  exactly the double-classification failure `376699b` existed to close.
- Worse, the current call shape holds the lease only for the duration of the *synchronous*
  `classify()` frame (`poller.py:427-429`). If `classify()` becomes fire-and-forget-plus-callback,
  `process_comment` returns immediately, `drain_pending`'s loop ends, and the lease is simply a
  5-minute timer with no worker behind it. Nothing in the code re-claims or extends it on callback
  arrival — **(unverified whether any renewal is intended; no such code exists today).**
- **What re-drives a comment whose callback never arrives:** *nothing, on the live path.* A
  `PendingComment` row is only ever created by `_enqueue_pending` (`poller.py:162`), which is called
  solely from failure branches: cap defer `poller.py:406`, classifier unavailable `poller.py:442`,
  hide failed `poller.py:488` and `poller.py:508`. On the happy webhook path a comment reaching
  `classify()` has **no pending row at all**. So if `classify()` becomes a bus submit and the callback
  is lost, the comment is dropped silently — no row, no dedup token (the `ClassifiedComment` insert at
  `poller.py:528` never runs), nothing to drain. Live-path entry is
  `background_tasks.add_task(poller.handle_webhook_comment, ...)` `app/routes/webhook.py:88-89` →
  `process_comment` `poller.py:950`.
- On the *drain* path a lost callback is recoverable but only by accident of expiry: the row survives
  (deletion at `poller.py:564-566` never runs), `claimed_at` stays set (nothing clears it —
  `_enqueue_pending` is not reached), and the row becomes claimable again 5 minutes later. Re-drive
  requires `attempts < MAX_RETRY_ATTEMPTS` (`poller.py:641`, `MAX_RETRY_ATTEMPTS = 5`
  `poller.py:159`) — and note a lost callback does **not** bump `attempts` (only `_enqueue_pending`
  does, `poller.py:206`), so such a row would re-drive indefinitely rather than give up.
- **Implication for llmbus:** the bus integration needs a pending row created *before* submit (not
  only on failure), and either a lease renewed on callback or a lease decoupled from the in-flight
  job. The repo's own doc already flags the adjacent open race: `drain_queue.py:29-43` — a cron drain
  of queued X racing a webhook redelivering X live, "the live path never touches the pending row, so
  there is nothing to claim," accepted as one wasted classification (~$0.0002) and one redundant
  `hide`. A bus makes that window minutes wide instead of milliseconds.

## 5. `app/scripts/drain_queue.py` — separate sync process, no event loop

- Entrypoint: `def main() -> None:` **`drain_queue.py:116`**; body `configure_logging(logging.INFO)`
  `:119` + `drain_all()` `:120`; `if __name__ == "__main__": main()` `:123-124`.
- `drain_all()` `drain_queue.py:58`, session `drain_queue.py:60`, per-user call
  `poller.drain_user_queue(db, user)` `drain_queue.py:93`, remaining count `drain_queue.py:103-107`,
  `db.close()` `drain_queue.py:113`.
- **No event loop:** `grep -n "asyncio\|async def\|await " app/scripts/drain_queue.py` → exit 1, no
  matches. Still a plain sync cron process (`uv run python -m app.scripts.drain_queue`,
  `drain_queue.py:5`), started outside FastAPI, so it gets **no lifespan and no BusClient**. The
  two-entrypoint problem from the old note §5 stands unchanged.
- It explicitly documents its own overlap handling via the lease: `drain_queue.py:22-27`.

## 6. FastAPI lifespan hook point

- `@asynccontextmanager` `app/main.py:220`; `async def lifespan(app: FastAPI) -> AsyncIterator[None]:`
  `main.py:221`; body `main.py:225-234` (`configure_logging` `225`, `error_emails.install()` `229`,
  `create_all` `230`, `_migrate_add_columns` `231`, `_migrate_add_indexes` `232`,
  `_sweep_token_encryption` `233`, `_purge_expired_comment_text` `234`); `yield` `main.py:235`.
- Registered: `app = FastAPI(title="hate-moderator", lifespan=lifespan)` `main.py:265`.
- Thread-hop justification unchanged: `process_comment` sync `poller.py:232`, reached off-thread via
  `webhook.py:88-89` and `threading.Thread(...)` `poller.py:150`; single-uvicorn-worker pin
  documented `poller.py:136`.

## 7. `/internal/*` routes — still ABSENT

Command run: `grep -rn "internal" app/ --include="*.py"` → **exit status 1, zero matches.** No
internal namespace, no bus-callback shared secret. Reusable auth patterns unchanged: HMAC verify
`app/routes/webhook.py:100-106` (digest `:105`, `hmac.compare_digest` `:106`), header read
`webhook.py:42`, 403 on mismatch `webhook.py:43`, verify-token compare `webhook.py:28`, signed
serializer `app/security/tokens.py:15` / `:24-25`.

## 8. `settings.llm_model` and cost rates

- `llm_model: str = "gpt-5.4-mini"` — `app/config.py:29`.
- `llm_cost_per_million_input_tokens_usd: float = 0.75` — `config.py:30`.
- `llm_cost_per_million_output_tokens_usd: float = 4.50` — `config.py:31`.
- Model-choice rationale (do not swap silently; rates MUST track the model) — `config.py:23-28`.
- `.env` loading: `env_file=".env"`, `case_sensitive=False` — `config.py:6-11`. **`.env` exists and is
  gitignored** (`git check-ignore .env` → `.env`). `grep -c "^LLM_MODEL" .env` → `0`, so **no
  `LLM_MODEL` override; the effective model is the default `gpt-5.4-mini`.** No secret values were
  read or printed.
- Cost math consumer: `classifier.cost_usd` `classifier.py:261`, applied `poller.py:544`.

## 9. Fail-open vs fail-closed split — CONFIRMED, unchanged

- **Fail-CLOSED (retry):** truncated (`finish_reason == "length"`) or empty → guard
  `classifier.py:192`, log `classifier.py:200-206`, `raise ClassifierIncompleteError`
  `classifier.py:207` (class `classifier.py:16`). Service errors re-raise `classifier.py:181-188`.
  Caller re-queues `poller.py:431-451`.
- **Fail-OPEN (neutral):** a *complete, non-empty* body that won't parse → `except`
  `classifier.py:228`, rationale `classifier.py:229-232`, `return "neutral", 0.0, 0, 0`
  `classifier.py:234`. Out-of-taxonomy category coerced to neutral `classifier.py:211-213`.
- **The split holds.** Only complete-but-garbage falls open. Preserve this when moving to
  `json_schema`: schema violation on a complete response → neutral; truncation/emptiness → retry.
</content>
