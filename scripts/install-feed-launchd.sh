#!/usr/bin/env bash
# Install a launchd job that runs the paced feed every N hours (macOS).
# Usage: scripts/install-feed-launchd.sh [INTERVAL_HOURS]   (default 1)
#   Uninstall: launchctl unload ~/Library/LaunchAgents/com.daily-agent.feed.plist
#
# The pacer caps how many chapters each run delivers and stays silent during
# quiet hours, so frequent runs trickle the backlog out. Bakes the current PATH
# into the job so `uv` resolves under launchd.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INTERVAL_HOURS="${1:-1}"
INTERVAL_SECONDS=$(( INTERVAL_HOURS * 3600 ))
LABEL="com.daily-agent.feed"
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
    <string>$PROJECT_DIR/scripts/run-feed.sh</string>
  </array>
  <key>StartInterval</key><integer>$INTERVAL_SECONDS</integer>
  <key>StandardOutPath</key><string>$PROJECT_DIR/digests/feed.out.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_DIR/digests/feed.err.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$PATH</string></dict>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed $LABEL — runs the paced feed every ${INTERVAL_HOURS}h."
echo "Plist:  $PLIST"
echo "Logs:   $PROJECT_DIR/digests/feed.{out,err}.log"
echo "Tune cadence via DAILY_AGENT_FEED_MAX_PER_RUN and DAILY_AGENT_FEED_QUIET_{START,END}."
echo "Test now: launchctl start $LABEL"
