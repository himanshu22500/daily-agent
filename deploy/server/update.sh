#!/usr/bin/env bash
# Deploy the checked-out branch on the feed server (issue #111) and restart it.
#
#   ssh ubuntu@daily-agent 'bash ~/daily-agent/deploy/server/update.sh [branch]'
#
# Pulls the branch (default main), syncs dependencies, installs/refreshes the
# systemd units, restarts the feed and points `tailscale serve` at it.
#
# Deploy-on-merge (issue #128) runs it with DEPLOY_SHA set: that exact commit
# (already checked out) instead of the branch's tip, which may have moved on
# without passing CI yet. This also installs and starts the timer that runs
# deploy-on-merge every 2 minutes; `daily-agent autodeploy pause` stops it.
set -euo pipefail
APP_DIR="${APP_DIR:-$HOME/daily-agent}"
BRANCH="${1:-main}"
UV="$HOME/.local/bin/uv"

cd "$APP_DIR"
if [ -n "${DEPLOY_SHA:-}" ]; then
  git reset -q --hard "$DEPLOY_SHA"
else
  git fetch -q origin
  git checkout -q "$BRANCH"
  git reset -q --hard "origin/$BRANCH"
fi
"$UV" sync -q --frozen --no-dev

sudo install -m 644 deploy/server/daily-agent-web.service \
  deploy/server/daily-agent-autodeploy.service \
  deploy/server/daily-agent-autodeploy.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable -q daily-agent-web
sudo systemctl enable -q --now daily-agent-autodeploy.timer
sudo systemctl restart daily-agent-web
if tailscale status >/dev/null 2>&1; then
  sudo tailscale serve --bg --http=80 http://127.0.0.1:8765 >/dev/null
else
  echo "tailscale is not up yet: run 'sudo tailscale up --hostname daily-agent', then this again" >&2
fi

for _ in $(seq 1 30); do
  # /new-count only reads; loading / would record a feed session.
  if curl -fs -o /dev/null "http://127.0.0.1:8765/new-count?since=2000-01-01T00:00:00"; then
    echo "feed is up: $(git rev-parse --short HEAD) on $BRANCH"
    exit 0
  fi
  sleep 1
done
echo "feed did not come up; see: journalctl -u daily-agent-web -n 50" >&2
exit 1
