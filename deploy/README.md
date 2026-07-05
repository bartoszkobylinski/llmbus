# Deploying llmbus to the VPS (no Docker)

Runbook for `izabela213` (Ubuntu 22.04, user `bartek`). Deploys **the Iggy broker**
(from source, systemd) and **the worker**. The producer half (`client.py`) is a
library other repos import — it has no service of its own.

**Facts this is built on** (checked on the box, not assumed):

| | |
|---|---|
| Iggy on the VPS | **none yet** — this stands it up |
| Iggy runtime | **binary from source, under systemd** — no Docker (§9b). *No prebuilt 0.8.0 server binary exists* (empty GitHub release assets, source-only Apache downloads), so it's a one-time Rust build. |
| Kernel | Iggy uses **io_uring → needs ≥ 5.19**. Ubuntu 22.04 stock is **5.15** — check first, bump via HWE if needed. |
| Iggy port | **127.0.0.1:8092** (8090 = beziarnia, 8091 = uvicorn already bound) |
| Service user / layout | `bartek`, code in `~/Projects/<name>`, `EnvironmentFile=.env` (matches beziarnia/milamber) |
| uv | `~/.local/bin/uv` (manages Python 3.13 — no system python3.13 needed) |

> This differs from §9b's original "prebuilt binary" sketch only in that 0.8.0 has no
> prebuilt binary, so we **build it once** from the `server-0.8.0` tag. §9b updated.

---

## 0. Kernel (do this first — gates everything)

```bash
uname -r
```
If it's `5.15.x`, get the HWE (6.x) kernel and reboot; if already `6.x`, skip:
```bash
sudo apt update && sudo apt install -y linux-generic-hwe-22.04 && sudo reboot
# after reboot, confirm:  uname -r  ->  6.x
```

## 1. Build the Iggy server from source (one-time)

No prebuilt 0.8.0 binary exists, so build it. The web UI must be built **before**
`cargo build` — the server embeds it (this is what the official Iggy Dockerfile does).

```bash
sudo apt update
sudo apt install -y git curl build-essential pkg-config libssl-dev libhwloc-dev \
    libudev-dev nodejs npm ca-certificates

curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"

git clone https://github.com/apache/iggy.git ~/src/iggy
cd ~/src/iggy
git checkout server-0.8.0

npm --prefix web ci
npm --prefix web run build:static
cargo build --bin iggy-server --release        # a few minutes; one-time

sudo install -m 0755 target/release/iggy-server /usr/local/bin/iggy-server
sudo mkdir -p /var/lib/iggy && sudo chown bartek:bartek /var/lib/iggy
```

## 2. Configure + start the broker

```bash
cd ~/Projects/llmbus
git pull                                       # get the deploy/ binary units

cp deploy/iggy.env.example deploy/iggy.env
nano deploy/iggy.env
#   set IGGY_ROOT_PASSWORD (e.g. `openssl rand -hex 24`); leave
#   IGGY_TCP_ADDRESS=127.0.0.1:8092 and IGGY_SYSTEM_PATH=/var/lib/iggy.

sudo cp deploy/iggy-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now iggy-server

systemctl status iggy-server --no-pager
journalctl -u iggy-server -n 30 --no-pager     # expect startup, no io_uring panic
ss -tlnp | grep 8092                            # expect it listening
```

## 3. Configure + start the worker

```bash
cd ~/Projects/llmbus
~/.local/bin/uv sync --extra worker            # already done at clone; safe to re-run

cp .env.example .env && chmod 600 .env
nano .env
#   OPENAI_API_KEY, ANTHROPIC_API_KEY
#   IGGY_ADDRESS=127.0.0.1:8092
#   IGGY_USERNAME / IGGY_PASSWORD  = the SAME as IGGY_ROOT_* in deploy/iggy.env
#   STORE_PATH=/home/bartek/Projects/llmbus/data/llmbus.db   (absolute)
#   OPENAI_RPM / OPENAI_TPM / ANTHROPIC_RPM / ANTHROPIC_TPM   (your provider limits)

sudo cp deploy/llmbus-worker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now llmbus-worker

journalctl -u llmbus-worker -f    # healthy: "worker consuming llmbus/llm-jobs as group llm-workers"
```

A freshly-deployed worker with no producer sending jobs just sits connected and idle —
that's the correct "up" state. Jobs flow once `hate-moderator` calls `bus.submit(...)`
(a separate change in that repo; §8, and §14 #3 for how it imports `llmbus`).

## Redeploys

- **Worker** (after a merge to main): `bash ~/Projects/llmbus/deploy/deploy.sh`
  (git pull + uv sync + restart worker). Iggy is untouched.
- **Iggy** (only to change version): rebuild §1 at the new tag, `sudo install …`, then
  `sudo systemctl restart iggy-server`. Data in `/var/lib/iggy` persists.

## Troubleshooting

- **Worker restart-loops, connect/login error** → Iggy isn't up or creds disagree.
  `systemctl status iggy-server`; check `.env` IGGY_USERNAME/PASSWORD == `deploy/iggy.env`.
- **`iggy-server` exits immediately** → kernel < 5.19 (see §0), or the unit is missing
  `AmbientCapabilities=CAP_SYS_NICE` / `LimitMEMLOCK=infinity`. `journalctl -u iggy-server`.
- **`cargo build` fails on the web assets** → the `npm --prefix web …` steps didn't run
  or failed; the server embeds that build.
- **`address already in use` on 8092** → pick another free port; update it in
  `deploy/iggy.env` (IGGY_TCP_ADDRESS) **and** the worker `.env` (IGGY_ADDRESS).
- **Reset the broker (wipe jobs + creds)** → `sudo systemctl stop iggy-server && sudo
  rm -rf /var/lib/iggy/* && sudo systemctl start iggy-server`.
