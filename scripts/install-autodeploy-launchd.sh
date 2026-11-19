#!/usr/bin/env bash
# Install deploy-on-merge on the Mac (issue #128): a launchd job that, every 2
# minutes, deploys main here once both CI checks pass and the feed server runs
# it (so the database backup and any schema change happen there first). It
# restarts com.daily-agent.web and rolls back if it doesn't come up. A run
# missed while the Mac sleeps happens on wake.
#
# Usage:     scripts/install-autodeploy-launchd.sh   (from the live install)
# Pause:     uv run daily-agent autodeploy pause      (resume: ... resume)
# Uninstall: launchctl unload ~/Library/LaunchAgents/com.daily-agent.autodeploy.plist
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LABEL="com.daily-agent.autodeploy"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
BIN="$PROJECT_DIR/.venv/bin/daily-agent"

if [ ! -x "$BIN" ]; then
  echo "No $BIN: run 'uv sync' in $PROJECT_DIR first." >&2
  exit 1
fi
if ! grep -q '^DAILY_AGENT_FEED_URL=' "$PROJECT_DIR/.env" 2>/dev/null; then
  echo "Set DAILY_AGENT_FEED_URL in $PROJECT_DIR/.env (the feed server's URL)." >&2
  exit 1
fi
mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/digests"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$BIN</string>
    <string>autodeploy</string>
    <string>run</string>
    <string>--role</string>
    <string>mac</string>
  </array>
  <key>WorkingDirectory</key><string>$PROJECT_DIR</string>
  <key>StartInterval</key><integer>120</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$PROJECT_DIR/digests/autodeploy.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_DIR/digests/autodeploy.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string></dict>
</dict>
</plist>
PLIST_EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed $LABEL: deploys main here every 2 minutes once CI passes."
echo "Log:    $PROJECT_DIR/digests/autodeploy.log"
echo "Status: uv run daily-agent autodeploy status"
