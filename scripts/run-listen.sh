#!/usr/bin/env bash
# Wrapper launchd (or you) can call to run the inbound follow-up listener.
# Resolves the project root from its own location, so it works regardless of cwd.
# This is a long-running process (long-polls Telegram); launchd KeepAlive keeps
# it up and restarts it if it exits.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

exec uv run daily-agent telegram-listen
