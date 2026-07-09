#!/usr/bin/env bash
# Install a launchd job that flushes insights after Claude transcript changes.
# Usage: scripts/install-insights-flush-launchd.sh [QUIET_SECONDS]   (default 30)
#   Uninstall: launchctl unload ~/Library/LaunchAgents/com.daily-agent.insights-flush.plist
#
# launchd watches filesystem changes, not a formal "Claude session closed" event.
# The wrapper waits until transcripts have been quiet for QUIET_SECONDS, then runs:
#   daily-agent insights flush --to-telegram
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
QUIET_SECONDS="${1:-30}"
LABEL="com.daily-agent.insights-flush"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

cd "$PROJECT_DIR"
TRANSCRIPTS_DIR="$(uv run python - <<'PY'
from daily_agent.config import get_settings
print(get_settings().transcripts_path)
PY
)"

mkdir -p "$HOME/Library/LaunchAgents" "$PROJECT_DIR/digests" "$TRANSCRIPTS_DIR"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PROJECT_DIR/scripts/run-insights-flush.sh</string>
    <string>--to-telegram</string>
    <string>--quiet-seconds</string><string>$QUIET_SECONDS</string>
  </array>
  <key>WatchPaths</key>
  <array>
    <string>$TRANSCRIPTS_DIR</string>
  </array>
  <key>StandardOutPath</key><string>$PROJECT_DIR/digests/insights-flush.out.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_DIR/digests/insights-flush.err.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$PATH</string></dict>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed $LABEL — watches Claude transcripts and flushes insights after ${QUIET_SECONDS}s quiet."
echo "Watching: $TRANSCRIPTS_DIR"
echo "Plist:    $PLIST"
echo "Logs:     $PROJECT_DIR/digests/insights-flush.{out,err}.log"
echo "Test now: launchctl start $LABEL"
