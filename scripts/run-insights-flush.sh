#!/usr/bin/env bash
# Wrapper launchd (or you) can call to flush Claude transcript insights.
# Resolves the project root from its own location, so it works regardless of cwd.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

exec uv run daily-agent insights flush "$@"
