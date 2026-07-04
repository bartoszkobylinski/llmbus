# Deploying llmbus to the VPS

Runbook for `izabela213` (Ubuntu 22.04, user `bartek`). Deploys **the worker** and its
**Iggy broker**. The producer half (`client.py`) is a library other repos import — it
has no service of its own.

**Facts this is built on** (from the box, not assumed):

| | |
|---|---|
| Iggy on the VPS | **none yet** — this stands it up |
| Iggy port | **8092** (8090 = beziarnia, 8091 = uvicorn already bound) |
| Iggy runtime | **Docker** (`apache/iggy:0.8.0`, same as CI) — Docker is installed, `bartek` is in the `docker` group |
| Service user / layout | `bartek`, code in `~/Projects/<name>`, `EnvironmentFile=.env` (matches beziarnia/milamber) |
| uv | `~/.local/bin/uv` (manages Python 3.13 — no system python3.13 needed) |

> Iggy runs in Docker here, which differs from ARCHITECTURE §9b's "binary under systemd"
> sketch — that section was written before the box had Iggy, and the box already runs
> Docker + a green CI Iggy. §9b has been updated to match.

## First-time setup

```bash
# 1. Clone
git clone https://github.com/bartoszkobylinski/llmbus.git ~/Projects/llmbus
cd ~/Projects/llmbus

# 2. Build the worker venv (worker extra = openai/anthropic/httpx; uv fetches Py 3.13)
~/.local/bin/uv sync --extra worker

# 3. Iggy broker credentials (gitignored)
cp deploy/iggy.env.example deploy/iggy.env
$EDITOR deploy/iggy.env          # set IGGY_ROOT_PASSWORD to something real

# 4. Worker config
cp .env.example .env
chmod 600 .env
$EDITOR .env
#   Set at minimum:
#     OPENAI_API_KEY, ANTHROPIC_API_KEY
#     IGGY_ADDRESS=127.0.0.1:8092
#     IGGY_USERNAME / IGGY_PASSWORD  = the SAME values as deploy/iggy.env
#     STORE_PATH=/home/bartek/Projects/llmbus/data/llmbus.db   (absolute)
#     OPENAI_RPM / OPENAI_TPM / ANTHROPIC_RPM / ANTHROPIC_TPM  (your provider limits)
#   WORKER_* defaults in .env.example are fine.

# 5. Start Iggy
docker compose -f deploy/docker-compose.prod.yml up -d
docker compose -f deploy/docker-compose.prod.yml logs --tail=20   # expect "started", no panic
ss -tlnp | grep 8092                                              # expect it listening

# 6. Install + start the worker service
sudo cp deploy/llmbus-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llmbus-worker

# 7. Verify
systemctl status llmbus-worker --no-pager
journalctl -u llmbus-worker -n 30 --no-pager
#   Healthy log line: "worker consuming llmbus/llm-jobs as group llm-workers"
```

A freshly-deployed worker with no producer sending jobs will simply sit connected and
idle — that is the correct "up" state. Jobs start flowing once `hate-moderator` calls
`bus.submit(...)` (a separate change in that repo; ARCHITECTURE §8, and §14 #3 for how
it imports `llmbus`).

## Redeploys (after a merge to main)

```bash
bash ~/Projects/llmbus/deploy/deploy.sh    # git pull + uv sync + restart worker
```

Iggy only needs redeploying to change its version/config:
`docker compose -f deploy/docker-compose.prod.yml up -d` (pulls/recreates; data volume persists).

## Troubleshooting

- **Worker restart-loops, logs show a connect/login error** → Iggy isn't up or creds
  disagree. Check `docker ps` and that `.env` IGGY_USERNAME/PASSWORD == `deploy/iggy.env`.
- **Iggy container exits immediately / panics on io_uring** → the `seccomp=unconfined`
  + `SYS_NICE` in the compose are missing or your Docker/kernel blocks io_uring.
- **`address already in use` on 8092** → something else grabbed it; pick another free
  port and update both the compose port mapping and `.env` IGGY_ADDRESS.
- **Reset the broker (wipe all jobs + creds)** →
  `docker compose -f deploy/docker-compose.prod.yml down -v` then `up -d`.
