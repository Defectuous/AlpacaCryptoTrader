#!/usr/bin/env bash
# Install AlpacaCryptoTrader as a systemd service.
#
# Run from the repo as the user the bot should run as (not root):
#     ./deploy/install_service.sh
# It will ask for sudo when it writes the unit file.
set -euo pipefail

SERVICE=alpacacryptotrader
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="$(id -un)"
UNIT_PATH="/etc/systemd/system/${SERVICE}.service"

if [[ "$RUN_USER" == "root" ]]; then
    echo "Run this as the user the bot should run as, not root (it uses sudo where needed)." >&2
    exit 1
fi

if [[ ! -f "$APP_DIR/.env" ]]; then
    echo "Missing $APP_DIR/.env - copy .env.example to .env and add your Alpaca keys first." >&2
    exit 1
fi

cd "$APP_DIR"

if [[ ! -x .venv/bin/python ]]; then
    echo "Creating virtualenv in $APP_DIR/.venv ..."
    python3 -m venv .venv
fi
echo "Installing requirements ..."
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet -r requirements.txt

mkdir -p logs
chmod 600 .env

echo "Writing $UNIT_PATH ..."
sed -e "s|__USER__|${RUN_USER}|g" -e "s|__APP_DIR__|${APP_DIR}|g" \
    deploy/${SERVICE}.service | sudo tee "$UNIT_PATH" > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable --now "$SERVICE"

echo
sudo systemctl --no-pager status "$SERVICE" || true
echo
echo "Installed. Follow the logs with:  journalctl -u $SERVICE -f"
