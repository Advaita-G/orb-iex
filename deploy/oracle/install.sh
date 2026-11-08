#!/usr/bin/env bash
# ORB-IEX: one-time setup on a fresh Oracle Linux 8/9 VM (user opc).
# Safe to run again. Places NO orders: it only installs, runs offline tests and
# a read-only Alpaca check. The service is enabled (starts at boot) but not
# started; start it with: orbctl start
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Advaita-G/alpaca-orb-bot.git}"
REPO_DIR="/home/opc/alpaca-orb-bot"
APP_DIR="$REPO_DIR/orb_iex"
DATA_DIR="/home/opc/orb_iex_data"
ENV_FILE="$DATA_DIR/.env"
SERVICE="orb-iex-paper"

if [ "$(id -un)" != "opc" ]; then
  echo "Run this as the opc user (it uses sudo where needed)." >&2
  exit 1
fi

echo "== packages"
sudo dnf -y install git python3.11 chrony >/dev/null
sudo systemctl enable --now chronyd >/dev/null          # accurate clock (09:35:03 scan, skew check)

echo "== code"
if [ -d "$REPO_DIR/.git" ]; then
  git -C "$REPO_DIR" fetch -q origin main
  git -C "$REPO_DIR" checkout -q main
  git -C "$REPO_DIR" pull -q --ff-only origin main
else
  git clone -q "$REPO_URL" "$REPO_DIR"
fi

echo "== python environment"
cd "$APP_DIR"
[ -d .venv ] || python3.11 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[dev]"

echo "== persistent data directory $DATA_DIR"
mkdir -p "$DATA_DIR"/{journal,logs,health,reports}
chmod 700 "$DATA_DIR"
if [ ! -f "$ENV_FILE" ]; then
  if [ -f "$APP_DIR/.env" ]; then
    cp "$APP_DIR/.env" "$ENV_FILE"                       # reuse an existing local .env
  else
    cp "$APP_DIR/.env.example" "$ENV_FILE"
    echo "   created $ENV_FILE from the example: put your PAPER keys in it"
  fi
fi
chmod 600 "$ENV_FILE"

# SELinux (enforcing by default on Oracle Linux): let systemd run the venv python.
if command -v getenforce >/dev/null && [ "$(getenforce)" = "Enforcing" ]; then
  sudo chcon -R -h -t bin_t "$APP_DIR/.venv/bin" || true
fi

echo "== offline tests"
.venv/bin/python -m pytest -q | tail -1

echo "== systemd service and journald limits"
sudo install -m 644 "$APP_DIR/deploy/oracle/$SERVICE.service" "/etc/systemd/system/$SERVICE.service"
sudo mkdir -p /etc/systemd/journald.conf.d
sudo install -m 644 "$APP_DIR/deploy/oracle/journald-orb-iex.conf" /etc/systemd/journald.conf.d/orb-iex.conf
sudo systemctl restart systemd-journald
sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE" >/dev/null
sudo install -m 755 "$APP_DIR/deploy/oracle/orbctl" /usr/local/bin/orbctl

echo "== read-only Alpaca check (no orders)"
if grep -Eq '^APCA_API_KEY_ID=.+' "$ENV_FILE" && grep -Eq '^APCA_API_SECRET_KEY=.+' "$ENV_FILE"; then
  .venv/bin/python -m orb_iex --config config/settings.oracle.toml --env-file "$ENV_FILE" \
    --require-private-env check-alpaca | tail -1
else
  echo "   skipped: add your PAPER keys to $ENV_FILE, then run: orbctl check"
fi

echo
echo "Setup done. The service starts at every boot. Start it now with: orbctl start"
