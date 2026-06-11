#!/usr/bin/env bash
# Install a launchd job that keeps the inbound follow-up listener running (macOS).
# Usage: scripts/install-listen-launchd.sh
#   Uninstall: launchctl unload ~/Library/LaunchAgents/com.daily-agent.listen.plist
#
# Unlike the feed job (which runs on an interval), the listener is a persistent
# process that long-polls Telegram, so this uses KeepAlive + RunAtLoad: launchd
# starts it at load and restarts it whenever it exits. Bakes the current PATH
# into the job so `uv` resolves under launchd.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LABEL="com.daily-agent.listen"
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
    <string>$PROJECT_DIR/scripts/run-listen.sh</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$PROJECT_DIR/digests/listen.out.log</string>
  <key>StandardErrorPath</key><string>$PROJECT_DIR/digests/listen.err.log</string>
  <key>EnvironmentVariables</key>
  <dict><key>PATH</key><string>$PATH</string></dict>
</dict>
</plist>
EOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "Installed $LABEL — keeps the follow-up listener running (restarts on exit)."
echo "Plist:  $PLIST"
echo "Logs:   $PROJECT_DIR/digests/listen.{out,err}.log"
echo "Stop:   launchctl unload $PLIST"
