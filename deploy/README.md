# Deploying llmbus to the VPS (no Docker)

Runbook for `izabela213` (Ubuntu 22.04, user `bartek`). Deploys **the Iggy broker**
(from source, systemd) and **the worker**. The producer half (`client.py`) is a
library other repos import — it has no service of its own.

**Facts this is built on** (verified on the box 2026-07-14, not assumed):

| | |
|---|---|
| Iggy on the VPS | **none yet** — this stands it up (no units, nothing on `:8092`) |
| Iggy runtime | **binary under systemd** — no Docker (§9b). *No prebuilt 0.8.0 server binary exists* (empty GitHub release assets, source-only Apache downloads), so we build it — **in CI, not here** (see below). |
| Kernel | **Nothing to do.** The box is an **LXC container** (`systemd-detect-virt` → `lxc`) on Proxmox host kernel **7.0.12-1-pve**, so it already clears Iggy's io_uring floor (≥ 5.19). io_uring is **verified working inside the container**: `kernel.io_uring_disabled=0` and a direct `io_uring_setup(2)` returns an fd. ⚠️ You **cannot** change the kernel from inside an LXC — `apt install linux-generic-hwe-*` is a no-op here. |
| Box size | **1 vCPU, 4 GiB RAM (~0.8 GiB free), zero swap.** This is why the Rust build happens in CI: a release link would OOM. |
| Iggy port | **127.0.0.1:8092** (8090 = beziarnia, 8091 = uvicorn already bound) |
| Runtime libs | **`libhwloc15` is MISSING** — install it (below). `libssl3`, `libudev1` already present. glibc **2.35**. |
| Service user / layout | `bartek`, code in `~/Projects/<name>`, `EnvironmentFile=.env` (matches beziarnia/milamber) |
| uv | `~/.local/bin/uv` (manages Python 3.13 — no system python3.13 needed) |
| sudo | prompts for a password — run the `sudo` blocks yourself |

---

## 0. Kernel — already satisfied, skip

Kept only to kill a wrong instruction that used to live here: *"Ubuntu 22.04 ships 5.15,
install the HWE kernel and reboot."* That is **false for this box**. It is an LXC
container — it runs the Proxmox **host's** kernel, and no package you install inside can
change that. The kernel rose to 7.0.12-1-pve on its own when the provider migrated the
container to a new host. Confirm and move on:

```bash
uname -r                                  # 7.0.12-1-pve  (>= 5.19 -> io_uring OK)
systemd-detect-virt                       # lxc
cat /proc/sys/kernel/io_uring_disabled    # 0  (2 would mean io_uring is off -> STOP)
```

## 1. Get the `iggy-server` binary (built in CI, not on the box)

The VPS cannot compile it (1 vCPU, ~0.8 GB free, no swap → OOM at link time), and no
prebuilt 0.8.0 binary is published. `.github/workflows/build-iggy-server.yml` builds it on
a runner inside an **`ubuntu:22.04` container** — that container is what makes the binary
loadable here: it links against **glibc 2.35**, the box's version. (`ubuntu-latest` is
24.04/glibc 2.39; that binary would not start.) It builds the web UI before `cargo`,
because the server embeds it.

**On your laptop** — build, fetch, ship:

```bash
cd ~/Programming/Python/llmbus
gh workflow run build-iggy-server.yml -f tag=server-0.8.0
RUN=$(gh run list --workflow=build-iggy-server.yml -L1 --json databaseId -q '.[0].databaseId')
gh run watch "$RUN" --exit-status          # ~15-25 min; the log's "Inspect binary" step
                                           # lists the exact libs the VPS needs
gh run download "$RUN" -n iggy-server-server-0.8.0-linux-x86_64 -D /tmp/iggy-bin
chmod +x /tmp/iggy-bin/target/release/iggy-server      # the artifact zip drops the exec bit
scp /tmp/iggy-bin/target/release/iggy-server bartek@100.124.41.86:/tmp/iggy-server
```

**On the VPS** — install it and prove it loads:

```bash
sudo apt update && sudo apt install -y libhwloc15        # Iggy links hwloc; not on the box
sudo install -m 0755 /tmp/iggy-server /usr/local/bin/iggy-server
sudo mkdir -p /var/lib/iggy && sudo chown bartek:bartek /var/lib/iggy

ldd /usr/local/bin/iggy-server | grep -i "not found" && echo "MISSING LIBS ^^" \
  || echo "all shared libs resolved"
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

## 4. Reading the cost ledger

`llmbus-costs` renders spend per project and day to a standalone HTML file. It reads only
the SQLite store — no Iggy, no API keys — so it is safe to run while the worker is up
(WAL: this reader never blocks that writer) *and* while it is down.

```bash
cd ~/Projects/llmbus && ~/.local/bin/uv run llmbus-costs --output /tmp/llmbus-costs.html
```

It picks up `STORE_PATH` from `.env`; pass `--store-path` to point it elsewhere. The page
is self-contained (inline CSS, no scripts, no fonts, no network), so pull it down and open
it locally:

```bash
scp bartek@izabela213:/tmp/llmbus-costs.html ~/Downloads/ && open ~/Downloads/llmbus-costs.html
```

A missing store is a hard error (exit 2), not an empty page — a typo'd path must not read
as "$0.00 spent". An *empty* store renders a page that says so explicitly.

## 5. Serving the ledger on the tailnet (for milamber's projects module)

`llmbus-costs-serve` serves the same page over HTTP, re-rendered per request. This is what
milamber's projects module links to.

**The page has no authentication — the tailnet is the access control.** That is why the
bind list must never include `0.0.0.0`.

Add to `.env`:

```bash
COSTS_BIND_HOSTS=127.0.0.1,100.124.41.86
COSTS_PORT=8093
```

Both addresses are load-bearing and it is not redundancy:

- `127.0.0.1` — milamber decides a card is "online" with `socket.create_connection(
  ("127.0.0.1", port))` (`api/routers/projects.py`). Without loopback the card shows a
  permanent false "Offline" (this is why `capcycle-web`, which binds only the tailnet IP,
  cannot show green).
- `100.124.41.86` — the card's link opens `http://<the host you are browsing milamber
  on>:<port>` (`api/templates/projects.html`). milamber does **not** proxy; the browser
  connects here directly.

Install and start:

```bash
cd ~/Projects/llmbus && git pull && ~/.local/bin/uv sync
sudo cp deploy/llmbus-costs.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now llmbus-costs
systemctl status llmbus-costs --no-pager
journalctl -u llmbus-costs -n 30 --no-pager   # healthy: "serving … on 127.0.0.1:8093, 100.124.41.86:8093"
```

Verify both paths the way milamber will:

```bash
curl -s -o /dev/null -w "loopback=%{http_code}\n" http://127.0.0.1:8093/
curl -s -o /dev/null -w "tailnet=%{http_code}\n"  http://100.124.41.86:8093/
```

Then register it in milamber (Projects → Add, or via the API) with **name** `llmbus`,
**port** `8093`. The card's link then opens the ledger in a new tab.

The unit is ordered `After=tailscaled.service` on purpose: `COSTS_BIND_HOSTS` names a
literal tailnet address, and binding it before `tailscale0` has that address fails with
`EADDRNOTAVAIL` — the same trap documented for `sites-enabled/nmed` and `capcycle-web`.

## 6. The model policy page (`/policy`)

`http://100.124.41.86:8093/policy` shows which model each `(project, kind)` runs on and
lets you change it. Unlike the cost ledger it is a **write** surface, so it is guarded.

Enable it by adding a secret to `.env` (unset = the page returns 503, not an open page):

```bash
ssh bartek@100.124.41.86 'cd ~/Projects/llmbus && printf "COSTS_AUTH_SECRET=%s\n" "$(openssl rand -hex 24)" >> .env && chmod 600 .env && sudo systemctl restart llmbus-costs'
```

Then browse to `/policy`; the browser prompts for credentials. **Any username works** — the
secret is the password. Read the secret back with:

```bash
ssh bartek@100.124.41.86 'grep "^COSTS_AUTH_SECRET=" ~/Projects/llmbus/.env'
```

**Rotating or revoking the secret needs a restart.** It is read once at startup and held
for the life of the process, so editing `.env` alone does NOT revoke access — anyone with
the old secret keeps it until `llmbus-costs` restarts:

```bash
ssh bartek@100.124.41.86 'cd ~/Projects/llmbus && sed -i "s|^COSTS_AUTH_SECRET=.*|COSTS_AUTH_SECRET=$(openssl rand -hex 24)|" .env && sudo systemctl restart llmbus-costs && grep "^COSTS_AUTH_SECRET=" .env'
```

Guards, and why each exists:

- **Basic auth**, constant-time compared. Base64 is encoding, not encryption — this is only
  acceptable because Tailscale encrypts the transport and the bind never includes a public
  interface. Do not expose this port.
- **Cross-origin POSTs are refused (403).** Browsers re-send cached Basic credentials
  automatically, so authentication alone would not stop another origin driving the endpoint.
- **The model is a dropdown of registered models only**, grouped by capability, and the add
  form opens on a placeholder rather than pre-selecting one. Adding a model or changing a
  price stays a code change with a verified rate — never editable here.

## Redeploys

- **Worker** (after a merge to main): `bash ~/Projects/llmbus/deploy/deploy.sh`
  (git pull + uv sync + restart worker). Iggy is untouched.
- **Iggy** (only to change version): re-run §1 with the new tag
  (`gh workflow run build-iggy-server.yml -f tag=server-0.8.1`), `sudo install …`, then
  `sudo systemctl restart iggy-server`. Data in `/var/lib/iggy` persists.

## Troubleshooting

- **Worker restart-loops, connect/login error** → Iggy isn't up or creds disagree.
  `systemctl status iggy-server`; check `.env` IGGY_USERNAME/PASSWORD == `deploy/iggy.env`.
- **`iggy-server` exits immediately** → `journalctl -u iggy-server`. Not the kernel (§0 —
  it's fine and you can't change it anyway). Likely: the unit lost
  `AmbientCapabilities=CAP_SYS_NICE` / `LimitMEMLOCK=infinity`, or systemd could not grant
  them in this LXC — if the log says the ambient caps failed, drop those two lines and
  retry (Iggy only uses them to raise thread priority / lock memory).
- **`error while loading shared libraries: libhwloc.so.15`** → `sudo apt install -y libhwloc15`.
- **`GLIBC_2.3x not found`** → the binary was built on the wrong base. It must come from
  the `ubuntu:22.04` container job (glibc 2.35), not a bare `ubuntu-latest` runner.
- **`address already in use` on 8092** → pick another free port; update it in
  `deploy/iggy.env` (IGGY_TCP_ADDRESS) **and** the worker `.env` (IGGY_ADDRESS).
- **Reset the broker (wipe jobs + creds)** → `sudo systemctl stop iggy-server && sudo
  rm -rf /var/lib/iggy/* && sudo systemctl start iggy-server`.
