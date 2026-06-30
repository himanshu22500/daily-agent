#!/usr/bin/env bash
# Install a launchd job that runs the daily digest each morning (macOS).
# Usage: scripts/install-launchd.sh [HOUR]    (HOUR 0-23, default 9)
#   Uninstall: launchctl unload ~/Library/LaunchAgents/com.daily-agent.daily.plist
#
# Bakes the *current* PATH into the job so `uv` resolves under launchd's minimal
# environment. Run this from your normal shell.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
HOUR="${1:-9}"
LABEL="com.daily-agent.daily"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/digests"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PROJECT_DIR/scripts/run-daily.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$PROJECT_DIR/digests/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_DIR/digests/launchd.err.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$PATH</string></dict>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed $LABEL — runs daily at ${HOUR}:00."
echo "Plist:  $PLIST"
echo "Logs:   $PROJECT_DIR/digests/launchd.{out,err}.log"
echo "Test now: launchctl start $LABEL"
