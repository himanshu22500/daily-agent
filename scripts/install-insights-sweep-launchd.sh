#!/usr/bin/env bash
# Install a launchd job that sweeps every project's Claude transcripts on a timer
# (macOS). Catches sessions that rarely end — e.g. Claude inside Zed over ACP —
# which the SessionEnd hook never flushes.
# Usage: scripts/install-insights-sweep-launchd.sh [INTERVAL_MINUTES]   (default 30)
#   Uninstall: launchctl unload ~/Library/LaunchAgents/com.daily-agent.insights-sweep.plist
#
# Each run reads transcripts written to since they were last read, skipping any
# written to in the last 10 minutes. New insights publish to the web feed at once;
# Telegram keeps its own pace (only the flush delivers). A run missed while the Mac
# sleeps happens on wake.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
INTERVAL_MINUTES="${1:-30}"
LABEL="com.daily-agent.insights-sweep"
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
    <string>$PROJECT_DIR/scripts/run-insights-sweep.sh</string>
  </array>
  <key>StartInterval</key><integer>$((INTERVAL_MINUTES * 60))</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$PROJECT_DIR/digests/sweep.out.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_DIR/digests/sweep.err.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$PATH</string></dict>
</dict>
</plist>
PLIST_EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed $LABEL — sweeps transcripts every $INTERVAL_MINUTES minutes."
echo "Plist:  $PLIST"
echo "Logs:   $PROJECT_DIR/digests/sweep.{out,err}.log"
echo "Check:  uv run daily-agent insights sweep --dry-run"
