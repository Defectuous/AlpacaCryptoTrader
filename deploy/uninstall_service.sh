#!/usr/bin/env bash
# Stop and remove the AlpacaCryptoTrader systemd service. Leaves the repo, .env and logs alone.
set -euo pipefail

SERVICE=alpacacryptotrader
UNIT_PATH="/etc/systemd/system/${SERVICE}.service"

sudo systemctl disable --now "$SERVICE" 2>/dev/null || true
sudo rm -f "$UNIT_PATH"
sudo systemctl daemon-reload
sudo systemctl reset-failed "$SERVICE" 2>/dev/null || true

echo "Removed $SERVICE service."
