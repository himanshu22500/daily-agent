#!/usr/bin/env bash
# Wrapper launchd (or you) can call to run the paced feed.
# Resolves the project root from its own location, so it works regardless of cwd.
# The pacer (DAILY_AGENT_FEED_MAX_PER_RUN + quiet hours) bounds each run, so
# running this frequently trickles the backlog out rather than flooding.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

exec uv run daily-agent feed "$@"
