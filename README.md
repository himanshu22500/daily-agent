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

### Gemini on Vertex AI

Instead of an API key, Gemini can run on a Google Cloud project you have
Vertex AI access to (billing on, Vertex AI API enabled). daily-agent runs
unattended after each session, so give it its own saved credentials in a
separate gcloud config, leaving your default credentials untouched:

```bash
CLOUDSDK_CONFIG="$HOME/.config/gcloud-daily-agent" gcloud auth application-default login
CLOUDSDK_CONFIG="$HOME/.config/gcloud-daily-agent" \
  gcloud auth application-default set-quota-project YOUR_PROJECT
```

Then in `.env` (and comment out `GEMINI_API_KEY`):

```bash
DAILY_AGENT_MODEL=google-cloud:gemini-3.8-flash
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT
GOOGLE_CLOUD_LOCATION=global
GOOGLE_APPLICATION_CREDENTIALS=$HOME/.config/gcloud-daily-agent/application_default_credentials.json
```

`gemini-3.8-flash` is served from the `global` location; regional endpoints
(e.g. `us-central1`) answered 404 when this was set up.

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

# Hand off insights that need a web-feed headline, then store what was written.
uv run daily-agent insights titles-export --out untitled.json
uv run daily-agent insights titles-import titles.json   # {"insight:key": "Headline"}
```

Add an explicit note during a coding session like this:

```text
insight: SQLite upserts should enforce the deduplication key in the database.
```

The extractor can also identify durable lessons without a marker. It assigns one
of `repo`, `technique`, `gotcha`, `architecture`, or `general`, plus a stable
key and tags. Telegram delivery creates one private channel per type on demand.

## Web feed

Read every insight like a news feed, laid out like Google Discover. Each card
has a generated cover (colored by type, showing the main topic and the project
it came from), a headline, a two-line teaser, and a source row: project, time,
♡ save, share, and ⋮ (hide, copy). Tap a card to open it like an article. It
shows the full text, the project and branch it was learned on, and the date,
with bookmark, text size (Aa), and share. ✕ or Back returns to the same spot in
the feed. An insight appears the moment it is stored. It doesn't wait for the
Telegram pacer, and reading it doesn't change Telegram delivery.

Covers are illustrations when an insight has one: an SVG per insight, stored
in the main database (`insight_covers`). They are drawn outside capture, either
by illustrator subagents and brought in with
`uv run daily-agent insights covers-import <dir> --label subagent:fable`, or by
the model with `uv run daily-agent insights covers-generate` (only insights
without a cover; `--model` to choose). Every SVG must pass a strict validator
first: static and self-contained, fixed 800×400 viewBox, no scripts, external
references, embedded images, or event handlers. Insights without an
illustration keep the generated gradient cover.

Headlines aren't written at capture time, and no model call is made for them.
`insights titles-export` lists the insights without one. Once a batch of
headlines has been written for them (e.g. by Claude subagents),
`insights titles-import` cleans and stores them: unknown keys are reported, and
existing headlines are kept unless you pass `--overwrite`. Until an insight has
a headline, its first sentence is shown.
Sharing uses the phone's share sheet over HTTPS; on the plain-`http` tailnet URL
it copies the text to the clipboard instead.

```bash
uv run daily-agent web            # http://127.0.0.1:8765/
scripts/install-web-launchd.sh    # keep it running in the background (macOS)
```

**For you** (the default) is different every time you open it. It leads with
unseen insights from the last few days, then the rest of your unseen backlog in
a fresh random order. Higher-scored insights tend to come earlier, and there are
never more than two of one type in a row. Insights you've already seen stay out
until they're due for a revisit: after 1 day, 3 days, 1 week, 3 weeks, then
every 2 months. Saved ones come back sooner and hidden ones never do. Revisits
are marked "↻ Revisit". When nothing is left, it says you're caught up instead
of repeating itself.

An insight counts as **seen** only after it has been on screen for about a
second; loading it isn't enough. A blue dot marks insights you haven't seen.

**Latest** is the chronological view, ordered by when an insight was stored. A
flush that catches up on older sessions still lands its insights at the top,
marked "from <date>". Search and tag links open in Latest. If the page is left
open, a pill appears when new insights arrive.

It binds to `127.0.0.1` and has **no login**, so don't expose it on a LAN or the
internet.

### On your phone (Tailscale)

Keep the server on loopback and let `tailscale serve` relay it to devices signed
into your tailnet only. This is not Funnel, so nothing is public:

```bash
tailscale serve --bg --http=80 http://127.0.0.1:8765   # persists across restarts
```

Then allow the Mac's tailnet names in `.env` and restart the job:

```bash
DAILY_AGENT_WEB_ALLOWED_HOSTS=<mac>.<tailnet>.ts.net,<mac>,<tailscale-ip>
launchctl kickstart -k gui/$(id -u)/com.daily-agent.web
```

On the phone (Tailscale app signed in to the same account), open
`http://<mac>/` and use "Add to Home Screen". WireGuard encrypts the traffic
end to end, so HTTP is fine inside the tailnet. If you enable HTTPS certificates
for the tailnet, use `tailscale serve --bg 8765` for an `https://` URL instead.
Remove the relay with `tailscale serve --http=80 off`. The Mac must be awake and
connected to Tailscale.

Avoid binding `DAILY_AGENT_WEB_HOST` to the Tailscale IP or `0.0.0.0`. The Tailscale
IP breaks `127.0.0.1` access and fails while Tailscale is down. `0.0.0.0` exposes
the feed to your LAN. The Host allowlist only guards against DNS rebinding; it is
not access control. In the phone's browser, "Add to Home Screen" opens it like an
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
