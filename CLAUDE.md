# llmbus

Central bus for all LLM traffic across my projects: producers submit jobs to Apache Iggy,
a worker pool calls OpenAI/Anthropic centrally (rate-limit, retry, cost), results return via
callback or poll. Full design: **ARCHITECTURE.md** (Polish, living doc — §14 lists open
decisions; check it before implementing anything it covers).

Pilot integration: `hate-moderator`. v1 scope is deliberately small — read §1 non-goals
before adding anything.

## Stack

- Python 3.13 · `uv` · in-project `.venv` · fully **async** (the SDK forces this)
- **`apache-iggy`** (PyPI, 0.8.x) — asyncio-only PyO3 binding over the Rust client.
  Never use the legacy `iggy-py` package.
- Pydantic (Job/Result schemas), SQLite (results store), `python-dotenv`, FastAPI-style idioms

## Iggy facts (verified against SDK 0.8.0 — re-verify on SDK upgrades)

- Hierarchy: **stream → topic → partition**. v1: stream `llmbus`, topic `llm-jobs`,
  1 partition, consumer group `llm-workers`.
- Local dev: `docker compose up -d` (image `apache/iggy`). Ports: TCP **8090** (primary),
  HTTP 3000, QUIC 8080. Root creds `iggy`/`iggy`.
- **NEVER point dev code at the prod VPS Iggy** (§9b) — dev uses its own local server via
  `IGGY_ADDRESS` in `.env`.
- **ALWAYS build the client with `IggyClient.from_connection_string("iggy+tcp://user:pass@host:port")`,
  never `IggyClient(addr)` + `login_user()`** (§14 #16). Auth is **per TCP session**, and every
  command goes through the SDK's `send_raw_with_response`, which on a transient error — *and on
  `Unauthenticated` itself* — silently does `disconnect()` → `connect()` → retry. Only the
  connection-string form sets `auto_login`, so only it re-authenticates on that reconnect; the
  manual form comes back unauthenticated and **cannot self-heal**. This cost an evening of prod
  debugging: it looks fine (login succeeds), then dies on a later command. Percent-encode the
  credentials. Note the SDK's **own** primary test fixture (`foreign/python/tests/conftest.py`)
  uses the unsafe manual form, so do not treat their examples as a guide here — their tests are
  short-lived against a fresh server and never reconnect. That footgun is worth reporting
  upstream (with the `leader_aware.rs` swallow that hides it, §14 #16), but our fix does not
  depend on them.
- Known SDK gaps this project hits: no **message headers** (→ metadata goes in the JSON
  body, §4) and no **get_stats** (→ lag approximated from the store, §11). Both are planned
  upstream contributions — use the documented workarounds, don't hack the binding locally.

## Commands

```bash
uv sync                                  # deps
docker compose up -d                     # local Iggy for dev/integration tests
uv run pytest -m "not integration"       # fast suite (no server needed)
uv run pytest                            # full suite (requires local Iggy)
uv run mutmut run                        # mutation testing (scoped, see below)
uv run mypy                              # static type gate (strict, src/ only)
uv run ruff check . && uv run ruff format --check .
```

## Layout (target, from ARCHITECTURE.md §9)

```
src/llmbus/
  client.py      # submit(), await_result() — imported by other projects
  schema.py      # Pydantic: Job, Result
  worker.py      # consumer group loop
  providers/     # base.py, openai.py, anthropic.py
  ratelimit.py   # token-bucket per provider
  cost.py        # usage → USD, per project
  store.py       # SQLite results
  config.py
tests/
  unit/          # pure logic, no network, no Iggy
  integration/   # @pytest.mark.integration — live dockerized Iggy
```

## Testing — the quality bar

This project's whole point is reliability of infrastructure code, so tests are not optional
polish; they gate every merge.

**Mandatory merge gate — all must pass, no exceptions:** `ruff check` + `ruff format --check`,
`mypy` (strict, `src/`), the full `pytest` suite, ≥90% coverage on `src/llmbus/`, and `mutmut`
**0 surviving mutants** on the scoped pure-logic modules. A branch is not merge-ready until
every one is green.

- **Unit tests** (`tests/unit/`): pure logic only — ratelimit, cost, schema, provider
  routing, idempotency. No network, no Iggy. Mock at *our* bus-abstraction boundary;
  the SDK's Rust-native classes cannot be deeply monkeypatched — never try.
- **Integration tests** (`tests/integration/`, marker `integration`): run against local
  dockerized Iggy. **Unique stream/topic names per test** (uuid suffix) — same isolation
  pattern the SDK's own tests use. Skip cleanly when the server is down.
- **pytest-asyncio** (`asyncio_mode = "auto"`) for everything async.
- **Coverage:** `pytest-cov`, fail under 90% on `src/llmbus/` (integration paths may be
  excluded with `# pragma: no cover` only when genuinely unreachable in unit runs).
- **Static typing (`mypy`):** `uv run mypy` (`--strict`, `src/` only) is a mandatory gate —
  zero errors. It is the *only* thing that enforces the **semantic** half of the `Provider`
  contract: that `call` is `async`, returns a `ProviderResult`, and `name` is a `str`.
  `@runtime_checkable` / `isinstance()` check attribute *presence* only, never shape (see
  `test_runtime_protocol_check_is_structural_not_semantic`). So concrete adapters must be
  reached through a typed seam (e.g. a `dict[str, Provider]` registry) for mypy to verify
  them, and carry per-adapter `await`/assert tests. Tests are excluded from mypy, mirroring
  the ruff `tests/** = ANN` ignore. The package ships a PEP 561 `py.typed` marker, so repos
  importing `llmbus` type-check against it too.
- **Mutation testing (`mutmut`):** scoped to pure-logic modules only —
  `schema.py`, `ratelimit.py`, `cost.py`, `providers/base.py`, routing logic.
  Never run mutations over integration-touching code (worker loop, store I/O, client) —
  each mutant would need a live server and the run explodes. Goal: **0 surviving mutants**
  in scoped modules before a feature branch is merge-ready.
- Test the failure paths first-class: 429/5xx retry, timeout, worker crash between model
  call and offset commit (idempotency via `job_id`), duplicate delivery.

## Rules specific to this repo

- Everything async end-to-end; no sync wrappers in v1.
- Message contract (§4) is the API — any change to Job/Result shape needs an
  ARCHITECTURE.md update in the same PR.
- Config only via `.env` / `config.py`; no hardcoded limits, models, or addresses.
- Open decisions live in ARCHITECTURE.md §14 — if implementation forces one, stop and ask,
  then record the answer there.

## Upstream SDK contributions (separate track)

Contributions to the Iggy Python SDK happen in a **separate clone** of
`github.com/apache/iggy`, dir `foreign/python/` — NOT in this repo.
It's Rust (PyO3) + hand-maintained `apache_iggy.pyi` stub + pytest against a live server.
Build there: `uv sync --all-extras` → `uv run maturin develop` → `uv run pytest tests/ -v`.
First targets: message headers, `get_stats` (the gaps in §12).
