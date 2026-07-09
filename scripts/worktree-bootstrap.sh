#!/usr/bin/env bash
#
# worktree-bootstrap.sh — make a fresh git worktree of daily-agent runnable.
#
# A new `git worktree` is a clean checkout: it has the branch's CODE but none of
# the gitignored local state (.env, .venv, team.json, telegram.session, the local
# SQLite DB, ...). This script fills those gaps, idempotently.
#
# USAGE
#   1. As a Zed `create_worktree` task hook (recommended) — Zed sets:
#        ZED_WORKTREE_ROOT = the new worktree's path
#      Just run this script with no args; it reads that var. See scripts/README
#      note below for the ~/.config/zed/tasks.json entry.
#
#   2. Standalone, from anywhere:
#        ./scripts/worktree-bootstrap.sh /path/to/new/worktree
#      The main checkout is auto-detected via `git worktree list`.
#
# DESIGN
#   - This is registered as a GLOBAL Zed create_worktree hook, which fires for
#     EVERY repo's worktrees. So it self-guards: if the repo isn't daily-agent it
#     exits 0 silently (Zed's `hide: on_success` then keeps the terminal hidden).
#   - Config/state files are COPIED (not symlinked) so each worktree can diverge
#     safely and can't corrupt the main checkout's state.
#   - The venv is REINSTALLED via `uv sync` (venv bin shebangs + pyvenv.cfg embed
#     absolute paths — a copied .venv would point back at the original worktree).
#     `uv sync` is near-instant off the shared package cache.
#   - Everything is skip-if-present, so re-running the hook is cheap.
#
set -uo pipefail

# ---------- pretty logging ----------
if [ -t 1 ]; then
  C_RESET=$'\033[0m'; C_DIM=$'\033[2m'; C_TEAL=$'\033[36m'
  C_AMBER=$'\033[33m'; C_RED=$'\033[31m'; C_BOLD=$'\033[1m'
else
  C_RESET=""; C_DIM=""; C_TEAL=""; C_AMBER=""; C_RED=""; C_BOLD=""
fi
step() { printf "%s▸ %s%s\n" "$C_TEAL" "$1" "$C_RESET"; }
ok()   { printf "  %s✓%s %s\n" "$C_TEAL" "$C_RESET" "$1"; }
skip() { printf "  %s· %s (already present)%s\n" "$C_DIM" "$1" "$C_RESET"; }
warn() { printf "  %s! %s%s\n" "$C_AMBER" "$1" "$C_RESET"; }
err()  { printf "  %s✗ %s%s\n" "$C_RED" "$1" "$C_RESET"; }
run()  { printf "  %s$ %s%s\n" "$C_DIM" "$*" "$C_RESET"; }

# ---------- resolve worktree + main checkout ----------
WT="${ZED_WORKTREE_ROOT:-${1:-}}"
if [ -z "$WT" ]; then
  err "No worktree path. Set ZED_WORKTREE_ROOT or pass it as arg 1."
  exit 1
fi
WT="$(cd "$WT" 2>/dev/null && pwd)" || { err "Worktree path does not exist: $WT"; exit 1; }

MAIN="${ZED_MAIN_GIT_WORKTREE:-}"
if [ -z "$MAIN" ]; then
  # First entry of `git worktree list` is always the main worktree.
  MAIN="$(git -C "$WT" worktree list --porcelain 2>/dev/null | awk '/^worktree /{print $2; exit}')"
fi
[ -n "$MAIN" ] && MAIN="$(cd "$MAIN" 2>/dev/null && pwd)"
if [ -z "$MAIN" ]; then
  err "Could not determine the main checkout for: $WT"
  exit 1
fi

REPO="$(basename "$MAIN")"

# ---------- self-guard: only act on daily-agent worktrees ----------
# This runs as a global Zed hook, so silently bow out for any other repo.
[ "$REPO" = "daily-agent" ] || exit 0

# Nothing to do if the "worktree" IS the main checkout (e.g. run by mistake).
if [ "$MAIN" = "$WT" ]; then
  warn "Target is the main checkout, not a worktree — nothing to bootstrap."
  exit 0
fi

printf "\n%s%s── daily-agent worktree bootstrap ─────────────────────%s\n" "$C_BOLD" "$C_TEAL" "$C_RESET"
printf "  worktree %s\n" "$WT"
printf "  main     %s\n\n" "$MAIN"

# ---------- helpers ----------
# copy_state <relpath> [required] : copy a gitignored file/dir from MAIN to WT if
# absent there. Pass "required" to escalate a missing source to an error.
copy_state() {
  local rel="$1" level="${2:-}" src="$MAIN/$1" dst="$WT/$1"
  if [ ! -e "$src" ]; then
    if [ "$level" = "required" ]; then
      err "$rel not found in main checkout — the worktree won't run without it."
    else
      printf "  %s· %s (none in main — skipped)%s\n" "$C_DIM" "$rel" "$C_RESET"
    fi
    return
  fi
  if [ -e "$dst" ]; then skip "$rel"; return; fi
  mkdir -p "$(dirname "$dst")"
  cp -R "$src" "$dst" && ok "copied $rel" || err "failed to copy $rel"
}

ensure_dir() { [ -d "$WT/$1" ] && skip "$1/" || { mkdir -p "$WT/$1" && ok "created $1/"; }; }

# ---------- config + secrets ----------
step "Config & secrets"
copy_state ".env" required             # all DAILY_AGENT_* config + provider API keys
copy_state "team.json"                 # PII name<->github map (brief/daily/--assignee me)
copy_state ".claude/settings.local.json"  # local Claude Code settings

# ---------- local state ----------
step "Local state"
# The SQLite store carries delivery-dedup + feed/listener state. Copying it (vs.
# starting empty) means a `feed` run in this worktree won't re-deliver the whole
# backlog to your Telegram. Each worktree gets its own independent copy.
copy_state "daily_agent.db"
copy_state "telegram.session"          # MTProto account login (multi-stream feed)
copy_state "telegram.session-journal"  # SQLite journal, if the session was mid-write
ensure_dir "digests"                   # daily digests get written here (PII — not copied)

# ---------- dependencies ----------
step "Dependencies"
if [ -d "$WT/.venv" ]; then
  skip ".venv"
elif command -v uv >/dev/null 2>&1; then
  run "uv sync"
  # dev group includes telethon, so MTProto (telegram-listen / multi-stream) works.
  ( cd "$WT" && uv sync ) && ok "uv sync complete (deps + dev group)" || err "uv sync failed"
else
  err "uv not on PATH — install it, then run: cd $WT && uv sync"
fi

printf "\n%s✓ worktree ready%s  " "$C_TEAL" "$C_RESET"
printf "%scd %q && uv run daily-agent --help%s\n" "$C_DIM" "$WT" "$C_RESET"
warn "Don't run 'daily-agent telegram-listen' here while the main listener runs —"
warn "Telegram allows only one getUpdates consumer per bot token."
printf "\n"
