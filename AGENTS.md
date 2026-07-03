# AGENTS.md — project instructions for Codex

`llmbus`: a central message bus for LLM traffic backed by Apache Iggy. Producers
submit jobs; a worker pool calls OpenAI/Anthropic centrally; results return via
callback or poll. Full design in **ARCHITECTURE.md** (Polish). Dev notes in **CLAUDE.md**.

## Your role on a PR

- **You own the tests.** Independently verify the change and write/strengthen tests
  for new behavior. Any tests already in the branch are a baseline — extend them and
  add cases the author missed; try to break the contract, don't just confirm it.
- **Do not modify source files to make tests pass.** If a test reveals a real bug,
  report it — don't paper over it.
- **Do not change features or scope.** Review and test only.

## Mock boundaries

- **Mock only external services:** the OpenAI/Anthropic HTTP APIs, and the Iggy
  network client (`apache_iggy`) — its classes are Rust-native and can't be deeply
  monkeypatched, so mock at *our* bus-abstraction boundary, never inside the binding.
- **Never mock internal functions or the results store** (SQLite). Exercise real logic.

## Conventions

- Python **3.13** (pinned — `apache-iggy` ships wheels only up to cp313), `uv`,
  fully **async**. Lint: `ruff` (config in `pyproject.toml`). Line length 100.
- **The message contract is the public API.** Any change to `Job`/`Result` shape
  (`src/llmbus/schema.py`) requires an ARCHITECTURE.md §4 update in the same PR.
- **Mutation testing** (`mutmut`) is scoped to pure-logic modules only
  (`[tool.mutmut] source_paths`); never over integration-touching code.
- Dev runs Iggy via `docker compose`; prod runs Iggy + worker under **systemd**
  (ARCHITECTURE.md §9b). No Docker in prod, no Dockerfile for the app.

## Commands

```bash
uv sync --frozen                      # install (mirrors CI)
uv run python -m pytest tests/ -v     # full suite
uv run pytest -m "not integration"    # unit only (no Iggy server)
uv run ruff check .
uv run mutmut run                      # mutation testing (scoped)
```

Integration tests (marker `integration`) need a local Iggy broker
(`docker compose up -d`) and use unique stream/topic names per test.
