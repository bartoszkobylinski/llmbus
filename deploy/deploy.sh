#!/usr/bin/env bash
# Redeploy the llmbus worker on the VPS: pull main, re-sync deps, restart.
# First-time setup is in deploy/README.md — this script assumes it is already done.
#
#   bash deploy/deploy.sh
set -euo pipefail

APP_DIR="${LLMBUS_DIR:-/home/bartek/Projects/llmbus}"
UV_BIN="${UV_BIN:-/home/bartek/.local/bin/uv}"
SERVICE="llmbus-worker.service"

cd "$APP_DIR"

echo "==> git pull --ff-only origin main"
git pull --ff-only origin main

echo "==> $UV_BIN sync --frozen --extra worker"
"$UV_BIN" sync --frozen --extra worker

echo "==> restart $SERVICE"
sudo systemctl restart "$SERVICE"
sleep 1
systemctl --no-pager --lines=15 status "$SERVICE" || true

echo
echo "Follow logs:  journalctl -u $SERVICE -f"
