# daily-agent

`daily-agent` is a private memory loop for lessons found while coding. It reads
local Claude Code JSONL transcripts, captures explicit `insight:` notes, uses
Gemini to extract other durable lessons, and stores them in SQLite. Every stored
insight shows up immediately on a local, scrollable **web feed**. Telegram
separately gets a small paced batch.

The useful output is a concise fact, gotcha, architecture constraint, or reusable
technique. Transcript chatter, progress updates, and temporary debugging steps are
filtered out. Exact semantic keys prevent the same lesson from being queued twice.

## Setup

```bash
uv sync
cp .env.example .env
# Add GEMINI_API_KEY to .env.
uv run pytest
```

The default model is `google:gemini-3.8-flash`. Transient provider failures use
bounded exponential backoff; authentication and other permanent failures stop
immediately.

By default, transcripts are read from the Claude Code project directory matching
this checkout. Set `DAILY_AGENT_INSIGHTS_TRANSCRIPTS_DIR` when the transcripts live
elsewhere.

## Use

```bash
# Capture marker notes and Gemini-extracted lessons.
uv run daily-agent insights collect

# Capture marker notes without calling an LLM.
uv run daily-agent insights collect --no-extract

# Queue new insights and resurface a paced batch in the terminal.
uv run daily-agent insights feed

# Deliver to a file or to private per-type Telegram channels.
uv run daily-agent insights feed --to-file digests/insights.md
uv run daily-agent insights feed --to-telegram

# Collect and deliver after transcript files settle.
uv run daily-agent insights flush --to-telegram

# Inspect captured and queued counts.
uv run daily-agent insights status
```

Add an explicit note during a coding session like this:

```text
insight: SQLite upserts should enforce the deduplication key in the database.
```

The extractor can also identify durable lessons without a marker. It assigns one
of `repo`, `technique`, `gotcha`, `architecture`, or `general`, plus a stable
key and tags. Telegram delivery creates one private channel per type on demand.

## Web feed

Read every insight like a news feed: newest first, infinite scroll, filters by
type, search, save, and hide. An insight appears the moment it is stored. It
doesn't wait for the Telegram pacer, and reading it doesn't change Telegram
delivery.

```bash
uv run daily-agent web            # http://127.0.0.1:8765/
scripts/install-web-launchd.sh    # keep it running in the background (macOS)
```

The feed is ordered by when an insight was stored. A flush that catches up on
older sessions still lands its insights at the top, marked "from <date>". A blue
dot marks insights you haven't scrolled past yet. If the page is left open, a
pill appears when new insights arrive.

It binds to `127.0.0.1` and has **no login**. To read it on your phone, set
`DAILY_AGENT_WEB_HOST` to a private interface you trust, such as your Mac's
Tailscale IP, and open it from the phone over Tailscale. Don't bind it to a
public network. The server only answers to loopback names and the bind host; to
use a Tailscale MagicDNS name, add it to `DAILY_AGENT_WEB_ALLOWED_HOSTS`. In the phone's browser, "Add to Home Screen" opens it like an
app.

## Telegram

Telegram is optional. Configure the bot token, API ID, API hash, and bot username
from `.env.example`, then authorize the local account session once:

```bash
uv run daily-agent telegram-auth
uv run daily-agent telegram-check
```

`telegram-check` sends a direct test message. Insight delivery creates channels
only when `--to-telegram` is used. Remove idle app-created channels with:

```bash
uv run daily-agent telegram-reap --idle-days 30
```

## Automation

On macOS, install the transcript watcher:

```bash
scripts/install-insights-flush-launchd.sh
```

It watches the configured transcript directory, waits for file activity to become
quiet, then runs `insights flush --to-telegram`. The flush lock and debounce stamp
prevent overlapping or duplicate runs.

Keep the web feed running with `scripts/install-web-launchd.sh`. Logs go to
`digests/web.{out,err}.log`; uninstall with
`launchctl unload ~/Library/LaunchAgents/com.daily-agent.web.plist`.

## Design

The pipeline is intentionally small:

```text
local transcripts -> marker/LLM extraction -> SQLite insight store
                  |-> web feed (reads the store directly; immediate)
                  `-> durable outbox -> pacer -> console/file/Telegram
```

All tests are offline. They use temporary SQLite databases, mocked HTTP, and fake
model agents. Secrets, transcripts, Telegram sessions, databases, and generated
output remain ignored by Git.
