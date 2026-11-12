#!/usr/bin/env bash
# One-time setup of the always-on feed server (issue #111).
#
# Run on a fresh Ubuntu 24.04 Lightsail instance as the default `ubuntu` user:
#
#   DB_PASSWORD=... bash bootstrap.sh
#
# Installs Postgres, Tailscale and uv, creates the `daily_agent` database, and
# lets Postgres accept connections only from localhost and the tailnet. The
# Lightsail firewall keeps every public port closed except SSH (and SSH can be
# closed too once Tailscale is up), so the database is reachable only over the
# tailnet. Safe to re-run.
set -euo pipefail

: "${DB_PASSWORD:?set DB_PASSWORD}"
DB_NAME="${DB_NAME:-daily_agent}"
DB_USER="${DB_USER:-daily_agent}"
REPO_URL="${REPO_URL:-https://github.com/himanshu22500/daily-agent.git}"
APP_DIR="${APP_DIR:-$HOME/daily-agent}"

# 1 GB of RAM is enough to serve the feed, but `uv sync` and Postgres together
# can spike; a small swap file keeps the OOM killer away.
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 1G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

sudo apt-get update -qq
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq postgresql git unattended-upgrades >/dev/null

command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
command -v uv >/dev/null || [ -x "$HOME/.local/bin/uv" ] || curl -LsSf https://astral.sh/uv/install.sh | sh

# --- Postgres -------------------------------------------------------------- #
PG_CONF_DIR="$(ls -d /etc/postgresql/*/main | sort -V | tail -1)"
sudo -u postgres psql -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$
BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '${DB_USER}') THEN
    CREATE ROLE ${DB_USER} LOGIN PASSWORD '${DB_PASSWORD}';
  ELSE
    ALTER ROLE ${DB_USER} PASSWORD '${DB_PASSWORD}';
  END IF;
END
\$\$;
SQL
# Byte-order collation ("C"), the same as SQLite's: the stores compare and sort
# ISO-timestamp strings and keys (feed cursors, watermarks), and the default
# en_US collation ignores punctuation, which would reorder them.
for db in "$DB_NAME" "${DB_NAME}_test"; do
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname = '$db'" | grep -q 1 \
    || sudo -u postgres createdb -O "$DB_USER" -E UTF8 -l C -T template0 "$db"
done

# Listen on every interface, but only accept localhost and tailnet clients.
# (Binding to the Tailscale IP alone fails at boot if Postgres starts first.)
sudo tee "$PG_CONF_DIR/conf.d/daily-agent.conf" >/dev/null <<'CONF'
listen_addresses = '*'
CONF
HBA="$PG_CONF_DIR/pg_hba.conf"
if ! sudo grep -q 'daily-agent tailnet' "$HBA"; then
  sudo tee -a "$HBA" >/dev/null <<HBA_LINES
# daily-agent tailnet (Tailscale CGNAT and ULA ranges)
host    all    ${DB_USER}    100.64.0.0/10          scram-sha-256
host    all    ${DB_USER}    fd7a:115c:a1e0::/48    scram-sha-256
HBA_LINES
fi
sudo systemctl restart postgresql

# --- App checkout ------------------------------------------------------------ #
if [ ! -d "$APP_DIR/.git" ]; then
  git clone -q "$REPO_URL" "$APP_DIR"
fi

echo "bootstrap done: Postgres $(basename "$(dirname "$PG_CONF_DIR")"), app in $APP_DIR"
echo "next: sudo tailscale up --hostname daily-agent --ssh=false"
