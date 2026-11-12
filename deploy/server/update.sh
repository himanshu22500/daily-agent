#!/usr/bin/env bash
# Deploy the checked-out branch on the feed server (issue #111) and restart it.
#
#   ssh ubuntu@daily-agent 'bash ~/daily-agent/deploy/server/update.sh [branch]'
#
# Pulls the branch (default main), syncs dependencies, installs/refreshes the
# systemd unit, restarts the feed and points `tailscale serve` at it.
set -euo pipefail
APP_DIR="${APP_DIR:-$HOME/daily-agent}"
BRANCH="${1:-main}"
UV="$HOME/.local/bin/uv"

cd "$APP_DIR"
git fetch -q origin
git checkout -q "$BRANCH"
git reset -q --hard "origin/$BRANCH"
"$UV" sync -q --frozen --no-dev

sudo install -m 644 deploy/server/daily-agent-web.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable -q daily-agent-web
sudo systemctl restart daily-agent-web
sudo tailscale serve --bg --http=80 http://127.0.0.1:8765 >/dev/null

for _ in $(seq 1 30); do
  if curl -fsS -o /dev/null http://127.0.0.1:8765/; then
    echo "feed is up: $(git rev-parse --short HEAD) on $BRANCH"
    exit 0
  fi
  sleep 1
done
echo "feed did not come up; see: journalctl -u daily-agent-web -n 50" >&2
exit 1
