# llmbus

Central message bus for all LLM traffic across my projects, backed by
[Apache Iggy](https://iggy.apache.org). Producers submit **jobs**; a worker pool
calls OpenAI/Anthropic centrally — one rate-limit, one retry policy, one cost
ledger, full audit/replay — and results return via callback or poll.

Design: **[ARCHITECTURE.md](./ARCHITECTURE.md)**. Agent/dev notes: **[CLAUDE.md](./CLAUDE.md)**.

## Status

Early v1 — **foundation + message contract** only. Not yet functional end-to-end.

## Dev setup

```bash
uv sync                                 # deps + in-project .venv
cp .env.example .env                    # fill in keys
docker compose up -d                    # local Iggy broker (dev only)

uv run pytest -m "not integration"      # fast unit suite (no server)
uv run pytest                           # full suite (needs local Iggy)
uv run ruff check . && uv run ruff format --check .
```

## Deployment

- **Dev (this laptop):** Iggy via `docker compose`.
- **Prod (VPS):** Iggy and the worker run as **systemd** units — no Docker
  (see ARCHITECTURE.md §9b). Unit files ship in a later `deploy/` PR.
