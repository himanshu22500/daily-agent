#!/usr/bin/env bash
# Install a launchd job that keeps the insight web feed running (macOS).
# Usage: scripts/install-web-launchd.sh
#   Uninstall: launchctl unload ~/Library/LaunchAgents/com.daily-agent.web.plist
#
# Host/port come from DAILY_AGENT_WEB_HOST / DAILY_AGENT_WEB_PORT in .env
# (default http://127.0.0.1:8765/). KeepAlive restarts the server if it exits.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LABEL="com.daily-agent.web"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/digests"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PROJECT_DIR/scripts/run-web.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PROJECT_DIR/digests/web.out.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_DIR/digests/web.err.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$PATH</string></dict>
</dict>
</plist>
PLIST_EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed $LABEL — keeps the insight web feed running."
echo "Plist:  $PLIST"
echo "Logs:   $PROJECT_DIR/digests/web.{out,err}.log"
echo "Open:   http://127.0.0.1:8765/ (or your DAILY_AGENT_WEB_HOST/PORT)"
